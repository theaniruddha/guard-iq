"""Red-team a committed contract: LLM-generated paraphrase + jailbreak probes
against every high-severity rule, scored by the real engine.

    uv run python scripts/run_redteam.py [policy_yaml]
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gc_forge.redteam.probe import run_redteam  # noqa: E402
from gc_forge.store import versioning as store  # noqa: E402


async def main() -> None:
    policy_path = sys.argv[1] if len(sys.argv) > 1 else "policies/food_service.v1.yaml"
    policy = store.load(policy_path)
    print(f"Red-teaming {policy.business_type} v{policy.version} ({policy_path})\n")

    report, updated = await run_redteam(policy)

    outcomes = Counter(r.outcome for r in report.results)
    print(f"{'rule':32} {'outcome':15} probe")
    print("-" * 100)
    for r in report.results:
        print(f"{r.rule_id:32} {r.outcome:15} {r.probe[:60]}")

    print("\nSummary:", dict(outcomes))
    if report.leaking_rules:
        print(f"LEAKING rules (needs_review reset to True): {report.leaking_rules}")
        out = Path(policy_path).with_name(Path(policy_path).stem + ".redteam.yaml")
        store.save(updated, out)
        print(f"Updated policy (flagged for re-review) written to {out}")
    else:
        print("No leaks — all probed rules held.")


if __name__ == "__main__":
    asyncio.run(main())
