#!/usr/bin/env python
"""
Warm-start demo (lean & simple).

Shows the evolution path described in CLAUDE.md:

1. You have a committed policy.
2. You have conversation logs (clusters where things went wrong).
3. You (or an orchestrator) propose ONE concrete rule change as a draft.
4. Use store.diff to see the delta.
5. Human reviews the diff (in real UI later).
6. New version committed.

This script uses the existing food_service.v1.yaml as base.
It does NOT call the LLM — it hard-codes a realistic example change
so the flow is fully runnable and easy to understand without API keys.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gc_forge.contract.schema import Bucket, ReviewStatus, Rule, Severity, Side
from gc_forge.engine import runtime as gc
from gc_forge.store import versioning as store

POLICY_PATH = Path("policies/food_service.v1.yaml")


def main() -> None:
    print("=== Warm Start Demo (small food_service use case) ===\n")

    # 1. Load committed contract
    old = store.load(POLICY_PATH)
    print(f"Loaded: {old.business_type} v{old.version}")
    print(f"Active input rules: {len([r for r in old.input_rules if r.review_status in (ReviewStatus.approved, ReviewStatus.edited)])}")
    print()

    # 2. Imagine logs showed a problem: users asking for "loyalty points" or
    #    "reward balance" and the bot was making up the rules.
    #    Orchestrator (lean) proposes exactly ONE new rule as draft.

    new_rule = Rule(
        id="output_loyalty_commitment",
        side=Side.output,
        bucket=Bucket.steer,
        intent='detect model reply promising loyalty points, rewards balance, or "you have X points"',
        message="I don't have access to loyalty balances. Let me connect you with the team who can check.",
        route_to="human_handoff",
        severity=Severity.medium,
        rationale="Bot was hallucinating reward balances from logs; must steer to humans.",
        source="log:loyalty-claims-2026-06",
        needs_review=True,
        review_status=ReviewStatus.draft,
    )

    # 3. Build a candidate new policy (copy + append the draft rule)
    new = old.model_copy(deep=True)
    new.output_rules.append(new_rule)
    # Note: in real warm-start the orchestrator would return an updated Policy with drafts

    # 4. Show the diff (this is what a human or review UI would look at)
    changes = store.diff(old, new)
    print("Proposed changes (diff):")
    for ch in changes:
        print("  ", ch)
    print()

    # 5. Bump version for the candidate (human will do real bump on commit)
    candidate = store.bump(new)
    candidate.knowledge_version = "warmstart:log-cluster"

    print(f"Candidate would be v{candidate.version}")
    print(f"New rule review_status: {new_rule.review_status}")
    print()

    # 6. Show that engine still works only on approved rules
    #    (the new draft is ignored until reviewed)
    decision = gc.enforce(old, "Congratulations, you earned 500 loyalty points!", side="output")
    print("With OLD policy on loyalty claim:", "PASS" if decision is gc.PASS else decision.bucket)

    # Even with candidate in memory, only approved matter for enforcement demo
    print("\n=== Warm-start flow complete (draft rule ready for Review HITL) ===")
    print("Next: in UI you would load the candidate, see the diff, edit/approve, then Commit.")


if __name__ == "__main__":
    main()