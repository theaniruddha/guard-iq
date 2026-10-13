from __future__ import annotations

"""
Warm-start pipeline: logs -> problem events -> group -> classify intent ->
bucket -> draft Rule per group -> candidate Policy + diff for Review.

Three parsing routes (backend):
    "local"   embed with hashed TF (offline)  -> greedy clustering -> classify
    "openai"  embed with OpenAI API           -> greedy clustering -> classify
    "llm"     no embeddings: one structured LLM call groups AND classifies

Classifier for the embedding routes: "llm" (default, falls back) or "heuristic".

Nothing here commits anything. The candidate policy carries the new rules as
review_status=draft; the engine ignores drafts, and the Review UI is the only
path to a committed version.
"""

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gc_forge.contract.schema import Bucket, Policy, ReviewStatus, Rule, Side
from gc_forge.engine import runtime as gc
from gc_forge.engine.runtime import _intent_matches
from gc_forge.store.versioning import diff as policy_diff
from gc_forge.warmstart.cache import JsonCache
from gc_forge.warmstart.classify import (
    RuleProposal,
    _quoted_phrases,
    classify_cluster,
    group_and_classify_llm,
)
from gc_forge.warmstart.cluster import cluster_events
from gc_forge.warmstart.embed import embed_with_cache, get_embedder
from gc_forge.warmstart.logs import ProblemEvent, extract_problem_events, load_turns


@dataclass
class ClusterReport:
    topic_slug: str
    size: int
    examples: list[str]
    signals: list[str]
    proposal: RuleProposal | None
    already_handled_by: str | None = None  # rule id that already fires on these asks
    drafted_rule_id: str | None = None
    coverage: float | None = None  # share of cluster examples the drafted rule matches


@dataclass
class WarmStartResult:
    candidate: Policy                       # old policy + draft rules appended
    changes: list[dict[str, Any]]           # store.diff(old, candidate)
    clusters: list[ClusterReport] = field(default_factory=list)
    n_turns: int = 0
    n_events: int = 0
    cache_stats: dict[str, int] = field(default_factory=dict)
    fallback_used: bool = False  # llm route failed -> local route took over
    fallback_reason: str | None = None


def _active_intents(policy: Policy) -> list[str]:
    return [
        r.intent
        for r in policy.input_rules + policy.output_rules
        if r.review_status in (ReviewStatus.approved, ReviewStatus.edited)
    ]


def _already_handled(policy: Policy, events: list[ProblemEvent]) -> str | None:
    """If the committed contract already fires on most of this cluster's asks,
    there is nothing to propose — report it instead of drafting a duplicate.
    Checks both sides: the user ask (input) and the bot's reply (output), since
    an existing output rule may already catch a hallucination cluster."""
    hits: dict[str, int] = {}
    sample = events[:5]
    for e in sample:
        # first rule to fire on either side "handles" this event (count once)
        for text, side in ((e.user_text, "input"), (e.assistant_text, "output")):
            if not text:
                continue
            d = gc.enforce(policy, text, side=side)
            if d is not gc.PASS:
                hits[d.matched_rule_id] = hits.get(d.matched_rule_id, 0) + 1
                break
    if not hits:
        return None
    rule_id, count = max(hits.items(), key=lambda kv: kv[1])
    return rule_id if count * 2 > len(sample) else None


def _unique_rule_id(policy: Policy, base: str) -> str:
    existing = {r.id for r in policy.input_rules + policy.output_rules}
    rule_id, n = base, 2
    while rule_id in existing:
        rule_id = f"{base}_{n}"
        n += 1
    return rule_id


_DEFAULT_MESSAGES = {
    Bucket.block: "I can't help with that request.",
    Bucket.steer: "That needs a team member — let me connect you with someone who can help.",
    Bucket.graceful_reject: "I'm not able to help with that here, but our team can — please reach out to them directly.",
}


def _ensure_enforceable(proposal: RuleProposal, members) -> tuple[RuleProposal, float]:
    """Make an LLM proposal actually work in the dumb keyword engine.

    - non-pass_through rules must carry a customer-facing message
    - steer rules need a route
    - the intent must match the very log lines it was mined from; if it
      doesn't, anchor it with quoted phrases taken from the cluster
    """
    if proposal.bucket != Bucket.pass_through and not (proposal.message or "").strip():
        proposal.message = _DEFAULT_MESSAGES[proposal.bucket]
    if proposal.bucket == Bucket.steer and not proposal.route_to:
        proposal.route_to = "human_handoff"

    texts = [m.user_text if proposal.side == Side.input else (m.assistant_text or m.user_text) for m in members]

    def coverage(intent: str) -> float:
        return sum(1 for t in texts if _intent_matches(t, intent)) / len(texts)

    cov = coverage(proposal.intent_label)
    if cov < 0.6:
        phrases = _quoted_phrases(members)
        if phrases:
            enriched = proposal.intent_label + " " + " ".join(f'"{p}"' for p in phrases)
            enriched_cov = coverage(enriched)
            if enriched_cov > cov:
                proposal.intent_label, cov = enriched, enriched_cov
    return proposal, cov


def _slugify(raw: str) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in raw.strip().lower())
    return "_".join(p for p in slug.split("_") if p) or "recurring_ask"


def _draft_rule(policy: Policy, proposal: RuleProposal, size: int) -> Rule:
    today = dt.date.today().isoformat()
    proposal.topic_slug = _slugify(proposal.topic_slug)
    rule_id = _unique_rule_id(policy, f"{proposal.side.value}_{proposal.topic_slug}")
    return Rule(
        id=rule_id,
        side=proposal.side,
        bucket=proposal.bucket,
        intent=proposal.intent_label,
        message=proposal.message,
        route_to=proposal.route_to,
        severity=proposal.severity,
        rationale=f"{proposal.rationale} ({size} occurrences in logs)",
        source=f"log:{proposal.topic_slug}-{today}",
        needs_review=True,
        review_status=ReviewStatus.draft,
    )


async def warm_start(
    policy: Policy,
    logs_path: str | Path,
    *,
    backend: str = "local",            # local | openai | llm
    classifier: str = "llm",           # llm | heuristic (embedding routes only)
    threshold: float | None = None,
    min_cluster_size: int = 2,
    cache_dir: str | Path = ".warmstart_cache",
    use_cache: bool = True,
) -> WarmStartResult:
    turns = load_turns(logs_path)
    events = extract_problem_events(turns)

    cache_dir = Path(cache_dir)
    embed_cache = JsonCache(cache_dir / "embeddings.json", enabled=use_cache)
    llm_cache = JsonCache(cache_dir / "llm.json", enabled=use_cache)

    candidate = policy.model_copy(deep=True)
    reports: list[ClusterReport] = []
    intents = _active_intents(policy)

    # 1. group events into recurring-problem clusters
    fell_back = False
    fallback_reason: str | None = None
    if backend == "llm":
        try:
            grouped = await group_and_classify_llm(events, policy.business_type, intents, llm_cache)
            clusters = [(idx, prop) for idx, prop in grouped]
        except Exception as exc:
            # never let a nightly job die on model flakiness — degrade to the
            # offline route; drafts still go through the same HITL review
            backend, fell_back = "local", True
            fallback_reason = repr(exc)
    if backend != "llm":
        embedder = get_embedder(backend)
        vectors = embed_with_cache(embedder, [e.key_text for e in events], embed_cache)
        thr = threshold if threshold is not None else embedder.default_threshold
        clusters = [
            (c.indices, None)
            for c in cluster_events(vectors, threshold=thr, min_size=min_cluster_size)
        ]

    # 2. per cluster: skip if already handled, else classify -> draft rule
    for indices, prefetched in clusters:
        members = [events[i] for i in indices]
        signals = sorted({s for e in members for s in e.signals})
        handled_by = _already_handled(policy, members)
        if handled_by:
            slug = prefetched.topic_slug if prefetched else "handled"
            reports.append(
                ClusterReport(
                    topic_slug=slug, size=len(members),
                    examples=[e.user_text for e in members[:3]],
                    signals=signals, proposal=prefetched,
                    already_handled_by=handled_by,
                )
            )
            continue

        proposal = prefetched or await classify_cluster(
            members, policy.business_type, intents, llm_cache, classifier=classifier
        )
        proposal, cov = _ensure_enforceable(proposal, members)
        rule = _draft_rule(candidate, proposal, len(members))
        (candidate.input_rules if rule.side == Side.input else candidate.output_rules).append(rule)
        reports.append(
            ClusterReport(
                topic_slug=proposal.topic_slug, size=len(members),
                examples=[e.user_text for e in members[:3]],
                signals=signals, proposal=proposal, drafted_rule_id=rule.id,
                coverage=cov,
            )
        )

    candidate.knowledge_version = f"{policy.knowledge_version}+warmstart:{dt.date.today().isoformat()}"

    embed_cache.save()
    llm_cache.save()

    return WarmStartResult(
        candidate=candidate,
        changes=policy_diff(policy, candidate),
        clusters=reports,
        n_turns=len(turns),
        n_events=len(events),
        fallback_used=fell_back,
        fallback_reason=fallback_reason,
        cache_stats={
            "embed_hits": embed_cache.hits, "embed_misses": embed_cache.misses,
            "llm_hits": llm_cache.hits, "llm_misses": llm_cache.misses,
        },
    )


def warm_start_sync(policy: Policy, logs_path: str | Path, **kwargs) -> WarmStartResult:
    return asyncio.run(warm_start(policy, logs_path, **kwargs))
