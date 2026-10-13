"""Nightly warm-start job (HITL preserved).

    python -m gc_forge.warmstart --policy policies/food_service.v1.yaml \
        --logs sample_logs/food_service_chats.jsonl --backend local --classifier heuristic

Writes a *candidate* YAML next to the policy (never overwrites the committed
one) and prints the cluster report + diff. A human then loads the candidate
in the Review UI to approve/edit/kill and commit.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from gc_forge.store import versioning as store
from gc_forge.warmstart.pipeline import warm_start_sync


def main() -> None:
    ap = argparse.ArgumentParser(description="Mine chat logs, propose draft rules (HITL).")
    ap.add_argument("--policy", required=True, help="committed policy YAML")
    ap.add_argument("--logs", required=True, help="chat logs JSONL")
    ap.add_argument("--backend", default="local", choices=["local", "openai", "llm"])
    ap.add_argument("--classifier", default="llm", choices=["llm", "heuristic"])
    ap.add_argument("--min-cluster-size", type=int, default=2)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", default=None, help="candidate YAML path (default: <policy>.candidate.yaml)")
    args = ap.parse_args()

    policy = store.load(args.policy)
    result = warm_start_sync(
        policy,
        args.logs,
        backend=args.backend,
        classifier=args.classifier,
        threshold=args.threshold,
        min_cluster_size=args.min_cluster_size,
        use_cache=not args.no_cache,
    )

    print(f"Turns: {result.n_turns} · problem events: {result.n_events} · clusters: {len(result.clusters)}")
    print(f"Cache: {result.cache_stats}")
    if result.fallback_used:
        print(f"NOTE: direct-LLM grouping failed ({result.fallback_reason}); fell back to local embeddings + LLM classifier.")
    print()
    for c in result.clusters:
        status = f"already handled by {c.already_handled_by}" if c.already_handled_by else f"drafted {c.drafted_rule_id}"
        print(f"[{c.size}x] {c.topic_slug} — {status}")
        if c.proposal and not c.already_handled_by:
            cov = f" coverage={c.coverage:.0%}" if c.coverage is not None else ""
            print(f"      bucket={c.proposal.bucket.value} side={c.proposal.side.value} sev={c.proposal.severity.value}{cov}")
        for ex in c.examples:
            print(f'      e.g. "{ex}"')

    adds = [ch for ch in result.changes if ch.get("change") == "add"]
    print(f"\nDiff vs committed: {len(adds)} rule(s) added (all draft — engine ignores until reviewed).")

    if adds:
        policy_path = Path(args.policy)
        out = Path(args.out) if args.out else policy_path.with_name(policy_path.stem + ".candidate.yaml")
        store.save(result.candidate, out)
        print(f"Candidate written: {out}")
        print("Next: open the Review UI, load this candidate, approve/edit/kill, commit.")
    else:
        print("Nothing new to propose.")


if __name__ == "__main__":
    main()
