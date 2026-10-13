"""Warm-start: mine chat logs for recurring unhandled problems and propose
draft rules as a reviewable diff. Never auto-applies — everything lands in
the same Review HITL surface as synthesis."""

from gc_forge.warmstart.pipeline import WarmStartResult, warm_start, warm_start_sync

__all__ = ["WarmStartResult", "warm_start", "warm_start_sync"]
