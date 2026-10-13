from __future__ import annotations

"""
Turn a cluster of problem events into ONE proposed rule.

Two classifiers:
    "llm"        — structured call (pydantic_ai, ModelRole.sub_agent) that
                   labels the intent behind the cluster and picks a bucket.
                   Falls back to heuristic on any failure.
    "heuristic"  — offline keyword mapping; good enough for nightly runs
                   without an API key, and the safety net for the LLM path.

Plus the third parsing route ("llm" backend in the pipeline): group AND
classify all events in a single structured LLM call, no embeddings at all.
"""

import json
import re
from typing import Optional

from pydantic import BaseModel, Field

from gc_forge.contract.schema import Bucket, Severity, Side
from gc_forge.warmstart.cache import JsonCache, content_key
from gc_forge.warmstart.logs import ProblemEvent


class RuleProposal(BaseModel):
    topic_slug: str = Field(description="short_snake_case topic, e.g. loyalty_points")
    intent_label: str = Field(
        description=(
            "One-line description of the behaviour to catch. MUST include 2-3 "
            'key phrases in double quotes taken from the examples, e.g. '
            'detect asks about "loyalty points" "reward balance" — the engine '
            "matches on these."
        )
    )
    side: Side
    bucket: Bucket
    severity: Severity
    message: Optional[str] = Field(
        default=None, description="Customer-facing reply for steer/graceful_reject/block"
    )
    route_to: Optional[str] = Field(default=None, description='e.g. "human_handoff" for steer')
    rationale: str = Field(description="Why this rule should exist, citing the log pattern")


class GroupedProposal(BaseModel):
    member_indices: list[int] = Field(description="0-based indices of the events in this group")
    proposal: RuleProposal


class TopicGrouping(BaseModel):
    groups: list[GroupedProposal] = Field(default_factory=list)


_CLASSIFY_SYSTEM = """You analyse recurring failures from a business chatbot's logs and propose ONE guardrail rule per failure group.

Bucket semantics (pick exactly one):
- block: abuse, jailbreaks, PII, anything the bot must hard-refuse.
- steer: legitimate ask the bot must NOT answer itself (billing, refunds, account data, medical/legal, anything needing verified info) -> hand off, set route_to="human_handoff" and write a warm message.
- graceful_reject: out-of-scope ask that recurs -> polite decline with a helpful pointer.
- pass_through: the ask is fine and SHOULD be answered; the bot just failed. Use when the fix is capability, not policy.

Side: "input" catches the user ask before the LLM; "output" catches a bad model reply (hallucinated commitments, made-up balances). If the failure is the bot inventing answers, prefer an output rule.

The enforcement engine is a dumb keyword/phrase matcher: quoted phrases in intent_label match literally, so quote the exact recurring words users typed. Messages are the business's voice: short, warm, no corporate boilerplate."""


def _agent():
    from pydantic_ai import Agent

    from gc_forge.config.models import ModelRole, get_model

    return Agent(
        get_model(ModelRole.sub_agent),
        system_prompt=_CLASSIFY_SYSTEM,
        output_type=RuleProposal,
        retries=3,
    )


def _grouping_agent():
    from pydantic_ai import Agent

    from gc_forge.config.models import ModelRole, get_model

    return Agent(
        get_model(ModelRole.sub_agent),
        system_prompt=_CLASSIFY_SYSTEM
        + "\n\nYou will receive a numbered list of failure events. Group events that express the SAME underlying ask (indices may appear in only one group), drop one-off noise (groups need >= 2 members), and propose one rule per group.",
        output_type=TopicGrouping,
        retries=3,
    )


def _cluster_prompt(events: list[ProblemEvent], business_type: str, existing_intents: list[str]) -> str:
    lines = [f"Business type: {business_type}", "", "Existing rules already cover:"]
    lines += [f"- {i}" for i in existing_intents] or ["(none)"]
    lines += ["", f"Recurring failure group ({len(events)} occurrences):"]
    for e in events[:8]:
        lines.append(f'- user: "{e.user_text}" | bot said: "{e.assistant_text[:120]}" | signals: {",".join(e.signals)}')
    lines.append("")
    lines.append("Propose ONE rule for this group. Do not duplicate an existing rule.")
    return "\n".join(lines)


# ── Heuristic fallback (offline) ─────────────────────────────────────────────

_ABUSE = re.compile(r"jailbreak|ignore (all|your) (previous|prior) instructions|\bDAN\b|no (rules|restrictions)", re.I)
_HANDOFF = re.compile(r"refund|money back|charge|bill|payment|account|complaint|manager|human|agent|cancel my|order.*(wrong|missing|late)", re.I)
_HALLUCINATION_SIGNAL = re.compile(r"points|balance|discount|promotion|guarantee|promised", re.I)

_WORD = re.compile(r"[a-z0-9]+")
_SLUG_STOP = frozenset("the and for with what how can you your our do does about have has this that".split())


def _slug_from(events: list[ProblemEvent]) -> str:
    counts: dict[str, int] = {}
    for e in events:
        for w in set(_WORD.findall(e.user_text.lower())):
            if len(w) > 3 and w not in _SLUG_STOP:
                counts[w] = counts.get(w, 0) + 1
    top = sorted(counts, key=lambda w: (-counts[w], w))[:2]
    return "_".join(top) if top else "recurring_ask"


def _quoted_phrases(events: list[ProblemEvent], max_phrases: int = 3) -> list[str]:
    """Most frequent bigrams across the cluster — the literal match anchors."""
    counts: dict[str, int] = {}
    for e in events:
        words = [w for w in _WORD.findall(e.user_text.lower()) if w not in _SLUG_STOP]
        for a, b in zip(words, words[1:]):
            if len(a) > 2 and len(b) > 2:
                counts[f"{a} {b}"] = counts.get(f"{a} {b}", 0) + 1
    ranked = sorted(counts, key=lambda p: (-counts[p], p))
    return ranked[:max_phrases]


def heuristic_proposal(events: list[ProblemEvent], business_type: str) -> RuleProposal:
    joined = " ".join(e.user_text for e in events)
    slug = _slug_from(events)
    phrases = _quoted_phrases(events)
    quoted = " ".join(f'"{p}"' for p in phrases) or f'"{slug.replace("_", " ")}"'
    n = len(events)

    if _ABUSE.search(joined):
        return RuleProposal(
            topic_slug=slug, side=Side.input, bucket=Bucket.block, severity=Severity.high,
            intent_label=f"detect abuse or jailbreak attempts like {quoted}",
            message="I can't help with that request.",
            rationale=f"{n} log occurrences show abuse/jailbreak attempts.",
        )
    if _HANDOFF.search(joined):
        return RuleProposal(
            topic_slug=slug, side=Side.input, bucket=Bucket.steer, severity=Severity.medium,
            intent_label=f"detect user asks needing a human, mentioning {quoted}",
            message="That needs a team member — let me connect you with someone who can help.",
            route_to="human_handoff",
            rationale=f"{n} log occurrences show this ask recurring; it needs verified info or authority the bot lacks.",
        )
    if _HALLUCINATION_SIGNAL.search(" ".join(e.assistant_text for e in events)):
        return RuleProposal(
            topic_slug=slug, side=Side.output, bucket=Bucket.steer, severity=Severity.medium,
            intent_label=f"detect model reply inventing commitments about {quoted}",
            message="I don't have verified information on that — let me connect you with the team.",
            route_to="human_handoff",
            rationale=f"{n} log occurrences suggest the bot answered without verified data.",
        )
    return RuleProposal(
        topic_slug=slug, side=Side.input, bucket=Bucket.graceful_reject, severity=Severity.medium,
        intent_label=f"detect recurring out-of-scope asks about {quoted}",
        message="I'm not able to help with that here, but our team can — reach out and they'll sort you out.",
        rationale=f"{n} log occurrences of the same unmet ask; nothing in the current contract handles it.",
    )


# ── LLM classification with cache + fallback ─────────────────────────────────

def _cache_key(events: list[ProblemEvent], business_type: str, tag: str) -> str:
    return content_key(tag, business_type, *(e.user_text + "|" + e.assistant_text for e in events))


async def classify_cluster(
    events: list[ProblemEvent],
    business_type: str,
    existing_intents: list[str],
    cache: JsonCache,
    classifier: str = "llm",
) -> RuleProposal:
    if classifier == "heuristic":
        return heuristic_proposal(events, business_type)

    key = _cache_key(events, business_type, "classify")
    cached = cache.get(key)
    if cached is not None:
        return RuleProposal.model_validate(cached)
    try:
        result = await _agent().run(_cluster_prompt(events, business_type, existing_intents))
        proposal = result.output
        cache.set(key, json.loads(proposal.model_dump_json()))
        return proposal
    except Exception:
        return heuristic_proposal(events, business_type)


async def group_and_classify_llm(
    events: list[ProblemEvent],
    business_type: str,
    existing_intents: list[str],
    cache: JsonCache,
) -> list[tuple[list[int], RuleProposal]]:
    """Direct-LLM route: no embeddings — one structured call groups the events
    into topics and proposes a rule per topic."""
    key = _cache_key(events, business_type, "group")
    cached = cache.get(key)
    if cached is not None:
        grouping = TopicGrouping.model_validate(cached)
    else:
        lines = [f"Business type: {business_type}", "", "Existing rules already cover:"]
        lines += [f"- {i}" for i in existing_intents] or ["(none)"]
        lines += ["", "Failure events:"]
        for i, e in enumerate(events):
            lines.append(f'[{i}] user: "{e.user_text}" | bot said: "{e.assistant_text[:120]}" | signals: {",".join(e.signals)}')
        result = await _grouping_agent().run("\n".join(lines))
        grouping = result.output
        cache.set(key, json.loads(grouping.model_dump_json()))

    out: list[tuple[list[int], RuleProposal]] = []
    for g in grouping.groups:
        valid = [i for i in g.member_indices if 0 <= i < len(events)]
        if len(valid) >= 2:
            out.append((valid, g.proposal))
    return out
