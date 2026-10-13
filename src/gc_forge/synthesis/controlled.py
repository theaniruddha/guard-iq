from __future__ import annotations

"""
Controlled synthesis — the opt-in, human-in-the-loop research path.

The automatic path (agent.py) lets the LLM call search/fetch mid-run.
Controlled mode splits that single run into three explicit steps so a human
can gate the research before any rules are drafted:

    1. plan_research()       LLM proposes queries + why (no searching yet)
    2. run_queries()         plain Python executes the human-approved queries
    3. draft_from_findings() LLM drafts the Policy from the gathered findings
                             (no tools — it sees only what the human approved)

agent.py is untouched; both paths share synthesis_brain.md, the taxonomy
seeds, and tools.search. The UI is expected to be a thin layer over these
three functions — approve/edit/skip/add happens between steps 1 and 2.
"""

import asyncio

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from gc_forge.config.models import ModelRole, get_model
from gc_forge.contract.schema import Policy
from gc_forge.synthesis import tools
from gc_forge.synthesis.agent import (
    _build_user_prompt,
    _load_seeds,
    _load_system_prompt,
    _match_archetype,
)
from gc_forge.synthesis.queries import banned_terms, sanitize_query, search_with_fallback


class PlannedQuery(BaseModel):
    query: str
    reason: str  # why this search shapes the rules (shown to the human)


class ResearchPlan(BaseModel):
    queries: list[PlannedQuery] = Field(default_factory=list)


class Finding(BaseModel):
    query: str                                # the human-approved query
    results: list[dict[str, str]] = Field(default_factory=list)
    effective_query: str | None = None        # what actually returned results
    fallback_stage: str | None = None         # jurisdiction_dropped | vertical_broadened


_PLAN_SUFFIX = (
    "\n\nDo NOT draft rules yet. Propose 4-8 web search queries you would run "
    "to research this domain's risks (regulations, common incidents, guardrail "
    "guidance). For each query give a one-sentence reason explaining which risk "
    "dimension it informs. A human will review, edit, and approve these queries "
    "before they are executed."
    "\n\nQuery construction rules (queries violating these will be rewritten):"
    "\n- Search at the business-TYPE level (e.g. 'quick-service restaurant', "
    "'sandwich chain'). NEVER include a brand/company name or a city/locality — "
    "those return zero results and violate the type-not-company design rule."
    "\n- Keep each query SPECIFIC on its risk dimension (allergen disclosure, "
    "promo/advertising rules, price commitments, health claims, employment/HR). "
    "Broaden the subject, not the dimension."
    "\n- Jurisdiction at province/state/country level is allowed where regulation "
    "is regional (food-safety, advertising, employment law); never city-level."
)

_DRAFT_SUFFIX = (
    "\n\nThe research below was gathered from human-approved search queries. "
    "Base your rules on these findings plus the taxonomy seeds; do not assume "
    "access to any other research."
)


def _plan_agent() -> Agent:
    return Agent(
        get_model(ModelRole.orchestrator),
        system_prompt=_load_system_prompt(),
        output_type=ResearchPlan,
    )


def _draft_agent() -> Agent:
    # Deliberately no tools: the draft sees only the approved findings.
    return Agent(
        get_model(ModelRole.orchestrator),
        system_prompt=_load_system_prompt(),
        output_type=Policy,
    )


def _type_term(business_type: str) -> str | None:
    """Type-level phrase used to replace a stripped brand token, when the
    input matches a known archetype (e.g. 'food service')."""
    archetype, seed_data = _match_archetype(business_type, _load_seeds())
    if seed_data.get("dimensions"):
        return archetype.replace("_", " ")
    return None


async def plan_research(business_type: str, context: str | None = None) -> ResearchPlan:
    """Step 1 — propose search queries with reasons. No searches are executed.
    Planned queries are sanitized deterministically: any brand/city token that
    leaked in from the raw business input is stripped before the human sees them."""
    prompt = _build_user_prompt(business_type, context) + _PLAN_SUFFIX
    result = await _plan_agent().run(prompt)
    plan = result.output

    type_term = _type_term(business_type)
    banned = banned_terms(business_type, context)
    sanitized: list[PlannedQuery] = []
    seen: set[str] = set()
    for q in plan.queries:
        clean = sanitize_query(q.query, banned, type_term)
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            sanitized.append(PlannedQuery(query=clean, reason=q.reason))
    plan.queries = sanitized or plan.queries
    return plan


def run_queries(queries: list[str], max_results: int = 5) -> list[Finding]:
    """Step 2 — execute the human-approved queries. Plain Python, no LLM.
    A query with 0 results is retried broader (drop jurisdiction, then
    generalize the vertical) so a risk dimension isn't lost to narrow phrasing."""
    findings: list[Finding] = []
    for query in queries:
        try:
            results, effective, stage = search_with_fallback(
                query, tools.search, max_results=max_results
            )
        except Exception as exc:
            results, effective, stage = (
                [{"title": "search failed", "url": "", "snippet": str(exc)}],
                query,
                "original",
            )
        findings.append(
            Finding(
                query=query,
                results=results,
                effective_query=effective if effective.lower() != query.lower() else None,
                fallback_stage=stage if stage not in ("original", "no_results") else None,
            )
        )
    return findings


def fetch_deeper(finding: Finding, url: str, max_chars: int = 4000) -> Finding:
    """Optionally pull full page content for one result (human-triggered).
    The excerpt is stored on the result dict and included in the draft prompt."""
    for r in finding.results:
        if r.get("url") == url:
            content = tools.fetch(url, max_chars=max_chars)
            r["content"] = content
            break
    return finding


def _findings_block(findings: list[Finding]) -> str:
    lines: list[str] = []
    for f in findings:
        lines.append(f"### Query: {f.query}")
        if not f.results:
            lines.append("(no results)")
        for r in f.results:
            lines.append(f"- {r.get('title', '')} ({r.get('url', '')})")
            snippet = r.get("snippet", "")
            if snippet:
                lines.append(f"  {snippet[:400]}")
            content = r.get("content", "")
            if content:
                lines.append(f"  Page content (fetched):\n  {content[:2000]}")
    return "\n".join(lines)


async def draft_from_findings(
    business_type: str,
    context: str | None,
    findings: list[Finding],
) -> Policy:
    """Step 3 — draft the Policy from approved findings (same retry shape as auto path)."""
    prompt = (
        _build_user_prompt(business_type, context)
        + _DRAFT_SUFFIX
        + "\n\n## Research findings\n"
        + _findings_block(findings)
    )
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            result = await _draft_agent().run(prompt)
            policy = result.output
            policy.business_type = business_type
            policy.intent = context
            if not policy.knowledge_version:
                policy.knowledge_version = "taxonomy:v1"
            return policy
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
    assert last_err is not None
    raise last_err
