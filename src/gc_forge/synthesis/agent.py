from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from pydantic_ai import Agent, RunContext

from gc_forge.config.models import ModelRole, get_model
from gc_forge.config.telemetry import configure_logfire
from gc_forge.contract.schema import Policy
from gc_forge.synthesis import tools
from gc_forge.synthesis.queries import banned_terms, sanitize_query, search_with_fallback

configure_logfire("guard-iq-synthesis")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BRAIN_PATH = _PROJECT_ROOT / "synthesis_brain.md"
_SEEDS_PATH = Path(__file__).resolve().parent / "taxonomy" / "seeds.yaml"


def _load_system_prompt() -> str:
    return _BRAIN_PATH.read_text()


def _load_seeds() -> dict:
    if not _SEEDS_PATH.exists():
        return {}
    return yaml.safe_load(_SEEDS_PATH.read_text()) or {}


def _match_archetype(business_type: str, seeds: dict) -> tuple[str, dict]:
    normalized = business_type.strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in seeds:
        return normalized, seeds[normalized]

    for key, value in seeds.items():
        if key.replace("_", " ") in business_type.lower() or key in normalized:
            return key, value

    return normalized, {"dimensions": {}}


def _build_user_prompt(business_type: str, context: str | None) -> str:
    seeds = _load_seeds()
    archetype, seed_data = _match_archetype(business_type, seeds)
    seed_yaml = yaml.safe_dump({archetype: seed_data}, sort_keys=False)
    context_line = context or "(none — broad domain sweep)"
    banned = sorted(banned_terms(business_type, context))
    banned_line = (
        "Research by business TYPE only. These proper nouns from the input are a "
        f"brand/place — NEVER put them in a search query: {', '.join(banned)}. "
        "Search the generic vertical instead (e.g. 'quick-service restaurant'), "
        "keeping each query specific on its risk dimension.\n\n"
        if banned
        else ""
    )
    return (
        f"Business: {business_type}\n"
        f"Matched archetype: {archetype}\n"
        f"Context: {context_line}\n\n"
        f"{banned_line}"
        f"Taxonomy seeds (floor, not ceiling):\n{seed_yaml}\n\n"
        "Research the domain risks. Be lean: draft a focused set of high-value "
        "input_rules and output_rules with clear buckets. Return a validated Policy."
    )


def _build_orchestrator(business_type: str = "", context: str | None = None) -> Agent:
    """Build the orchestrator agent. Constructed per synthesis call so that
    GC_MODEL_ORCHESTRATOR overrides (e.g. from the UI sidebar) apply without
    a restart. The search tool enforces query hygiene deterministically:
    brand/city tokens from the raw input are stripped, and 0-result queries
    retry broader (drop jurisdiction, then generalize the vertical)."""
    banned = banned_terms(business_type, context)

    agent = Agent(
        get_model(ModelRole.orchestrator),
        system_prompt=_load_system_prompt(),
        output_type=Policy,
    )

    @agent.tool
    def search(ctx: RunContext[None], query: str) -> list[dict[str, str]]:
        """Search the web for domain risks, regulations, and guardrail guidance."""
        clean = sanitize_query(query, banned)
        results, _effective, _stage = search_with_fallback(clean or query, tools.search)
        return results

    @agent.tool
    def fetch(ctx: RunContext[None], url: str) -> str:
        """Fetch a URL for deeper reading during research."""
        return tools.fetch(url)

    return agent


# Module-level instance kept for backwards compatibility / introspection.
orchestrator_agent = _build_orchestrator()


async def synthesize(business_type: str, context: str | None = None) -> Policy:
    """Run the orchestrator (lean: simple retry loop, max 3 attempts)."""
    orchestrator = _build_orchestrator(business_type, context)
    prompt = _build_user_prompt(business_type, context)
    last_err: Exception | None = None
    for attempt in range(3):  # max 2-3 retries as per lean orchestration
        try:
            result = await orchestrator.run(prompt)
            policy = result.output
            policy.business_type = business_type
            policy.intent = context  # stored in the fixed schema field
            if not policy.knowledge_version:
                policy.knowledge_version = "taxonomy:v1"
            return policy
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                # simple loop engineering - small backoff
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
    assert last_err is not None
    raise last_err