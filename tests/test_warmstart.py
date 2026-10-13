"""Warm-start pipeline tests — fully offline (local backend, heuristic classifier)."""

from pathlib import Path

from gc_forge.contract.schema import Bucket, ReviewStatus, Side
from gc_forge.engine import runtime as gc
from gc_forge.store.versioning import load
from gc_forge.warmstart.cache import JsonCache, content_key
from gc_forge.warmstart.classify import heuristic_proposal
from gc_forge.warmstart.cluster import cluster_events
from gc_forge.warmstart.embed import LocalEmbedder, cosine, embed_with_cache
from gc_forge.warmstart.logs import extract_problem_events, load_turns
from gc_forge.warmstart.pipeline import warm_start_sync

LOGS = Path("sample_logs/food_service_chats.jsonl")
POLICY = Path("policies/food_service.v1.yaml")


# ── logs / problem detection ─────────────────────────────────────────────────

def test_load_turns_and_extract_events():
    turns = load_turns(LOGS)
    assert len(turns) > 20
    events = extract_problem_events(turns)
    # benign conversations (c11-c13) must not produce events
    assert all(e.conversation_id not in ("c11", "c12", "c13") for e in events)
    # loyalty, catering, delivery, refund asks must all be caught
    joined = " ".join(e.user_text.lower() for e in events)
    for expected in ("loyalty", "catering", "deliver", "refund"):
        assert expected in joined, f"missing problem theme: {expected}"


def test_signals_capture_why():
    events = extract_problem_events(load_turns(LOGS))
    by_conv: dict[str, object] = {}
    for e in events:  # first (primary) event per conversation
        by_conv.setdefault(e.conversation_id, e)
    assert "explicitly_flagged" in by_conv["c02"].signals
    assert "assistant_deflected" in by_conv["c04"].signals
    assert "user_pushed_back" in by_conv["c03"].signals


# ── embeddings / clustering ──────────────────────────────────────────────────

def test_local_embedder_groups_similar_texts():
    emb = LocalEmbedder()
    vecs = emb.embed(
        [
            "how many loyalty points do I have",
            "check my loyalty point balance",
            "do you deliver to Maplewood",
        ]
    )
    assert cosine(vecs[0], vecs[1]) > cosine(vecs[0], vecs[2])


def test_clustering_separates_topics():
    emb = LocalEmbedder()
    texts = [
        "how many loyalty points do I have",
        "what is my loyalty points balance",
        "check my loyalty point balance please",
        "do you cater large parties catering",
        "catering order for an office event",
        "one-off unrelated question about parking",
    ]
    clusters = cluster_events(emb.embed(texts), threshold=emb.default_threshold, min_size=2)
    assert len(clusters) == 2  # loyalty + catering; the one-off is dropped
    sizes = sorted(len(c.indices) for c in clusters)
    assert sizes == [2, 3]


# ── heuristic intent -> bucket ───────────────────────────────────────────────

def test_heuristic_buckets():
    events = extract_problem_events(load_turns(LOGS))
    catering = [e for e in events if "cater" in e.user_text.lower()]
    prop = heuristic_proposal(catering, "food_service")
    assert prop.bucket in (Bucket.steer, Bucket.graceful_reject)
    assert '"' in prop.intent_label  # quoted phrases for the keyword engine


# ── full pipeline ────────────────────────────────────────────────────────────

def test_pipeline_end_to_end_offline(tmp_path):
    policy = load(POLICY)
    result = warm_start_sync(
        policy, LOGS, backend="local", classifier="heuristic", cache_dir=tmp_path
    )

    assert result.n_events >= 6
    assert result.clusters, "expected recurring problem clusters"

    # refund asks are already covered by input_refund_request — no duplicate rule
    handled = [c for c in result.clusters if c.already_handled_by]
    assert any(c.already_handled_by == "input_refund_request" for c in handled)

    # new drafts exist for the uncovered themes and appear in the diff
    drafted = [c for c in result.clusters if c.drafted_rule_id]
    assert drafted, "expected at least one drafted rule"
    adds = [ch for ch in result.changes if ch.get("change") == "add"]
    assert {c.drafted_rule_id for c in drafted} == {ch["rule_id"] for ch in adds}

    # every draft is inert: review_status=draft, engine ignores it
    new_rules = [
        r for r in result.candidate.input_rules + result.candidate.output_rules
        if r.review_status == ReviewStatus.draft
    ]
    assert len(new_rules) == len(drafted)
    for rule in new_rules:
        assert rule.needs_review
        assert rule.source.startswith("log:")
    d = gc.enforce(result.candidate, "Can you check my loyalty point balance", side="input")
    if d is not gc.PASS:
        assert d.matched_rule_id not in {r.id for r in new_rules}

    # committed policy untouched
    assert len(policy.input_rules) == 3 and len(policy.output_rules) == 2


def test_pipeline_cache_hits_on_rerun(tmp_path):
    policy = load(POLICY)
    r1 = warm_start_sync(policy, LOGS, backend="local", classifier="heuristic", cache_dir=tmp_path)
    assert r1.cache_stats["embed_misses"] > 0
    assert r1.cache_stats["embed_hits"] == 0

    r2 = warm_start_sync(policy, LOGS, backend="local", classifier="heuristic", cache_dir=tmp_path)
    assert r2.cache_stats["embed_misses"] == 0
    assert r2.cache_stats["embed_hits"] == r1.cache_stats["embed_misses"]
    # same proposals either way
    assert {c.topic_slug for c in r1.clusters} == {c.topic_slug for c in r2.clusters}


def test_cache_roundtrip(tmp_path):
    path = tmp_path / "c.json"
    c = JsonCache(path)
    key = content_key("a", "b")
    assert c.get(key) is None
    c.set(key, {"x": 1})
    c.save()
    c2 = JsonCache(path)
    assert c2.get(key) == {"x": 1}


def test_openai_backend_cached_vectors_skip_api(tmp_path, monkeypatch):
    """With everything cached, the openai backend must not call the network."""
    from gc_forge.warmstart import embed as embed_mod

    class FakeOpenAI:
        name = "openai"
        model = "text-embedding-3-small"
        default_threshold = 0.55
        calls = 0

        def embed(self, texts):
            FakeOpenAI.calls += 1
            return LocalEmbedder().embed(texts)

    cache = JsonCache(tmp_path / "emb.json")
    fake = FakeOpenAI()
    texts = ["alpha beta", "gamma delta"]
    embed_with_cache(fake, texts, cache)
    assert FakeOpenAI.calls == 1
    embed_with_cache(fake, texts, cache)
    assert FakeOpenAI.calls == 1  # second run fully cached
