# AGENTS.md — Guard-IQ / Guardrail Contract Forge

Instructions for AI coding agents working in this repository. Read this first.
`CLAUDE.md` is the original design contract, `IMPLEMENTATION_GUIDE.md` is the
operator guide, and `synthesis_brain.md` / `AGENT_SPEC.md` are the synthesis
agent's system prompt (not agent instructions for you). Where `CLAUDE.md` and
the code disagree, the code wins — this file describes the code as it is.

## What this project does

Turn a **business type** (+ optional intent/context) into a **reviewed,
versioned guardrail contract** (YAML), then enforce that contract
deterministically on both sides of a chat:

```
business type ──► Synthesis (LLM, authoring-time) ──► draft Policy
chat logs ──────► Warm-start (log mining) ──────────► draft rule diff
                              │
                              ▼
                    Review UI (HITL) — ONLY path to commit
                              │
                              ▼
                    policies/<type>.vN.yaml
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
     Engine library (gc.enforce)      Red-team probes (leak check)
```

It is **not** a chatbot platform, **not** a PII detector (PII is delegated to
Presidio), and **not** a live self-modifying policy. Evolution is always a
human-approved diff.

## Non-negotiable invariants

1. **Single source of truth:** `src/gc_forge/contract/schema.py` (`Policy` /
   `Rule`). Never duplicate the rule shape; import it everywhere.
2. **Only Review commits.** Synthesis and warm-start produce `draft` rules only.
   Nothing auto-applies.
3. **Engine enforces only `approved` / `edited` rules.** `draft` and `rejected`
   are inert by construction — do not weaken this.
4. **Engine stays dumb.** `engine/runtime.py` is a pure function of
   (contract, text): no LLM, no network, no live system state. Smarter
   behaviour belongs in better rules, not engine ML.
5. **PII is always delegated.** Rules set `pii_delegate: presidio:...`; the
   engine never detects PII itself and PII-delegate rules never fire via
   keyword matching.
6. **Synthesis sees a business type, never a company name.** Brand/city tokens
   are stripped from search queries (`synthesis/queries.py`).
7. **Contracts are versioned files:** `policies/<slug>.v<N>.yaml`. Warm-start
   candidates (`*.candidate.yaml`) and red-team outputs (`*.redteam.yaml`)
   never overwrite a committed version.

## Repository map

```
src/gc_forge/
  contract/schema.py        Policy + Rule, closed enums — THE interface
  contract/__init__.py      Re-exports schema symbols
  config/models.py          Per-role model routing (config/models.yaml +
                            GC_MODEL_<ROLE> env overrides), Settings
  config/telemetry.py       Logfire setup (no-op without LOGFIRE_TOKEN)
  store/versioning.py       save / load / bump / diff / default_policy_path
  engine/runtime.py         gc.load(), gc.enforce() → Decision | PASS (core)
  engine/service.py         Optional FastAPI sidecar over runtime (gc-engine)
  synthesis/agent.py        Automatic synthesis: Pydantic AI orchestrator
                            (system prompt from synthesis_brain.md, seeds
                            from taxonomy/seeds.yaml, tools = search/fetch)
  synthesis/controlled.py   Controlled synthesis: plan_research() →
                            run_queries() → draft_from_findings() (HITL gates
                            the research; draft agent has no tools)
  synthesis/queries.py      Query hygiene: banned_terms, sanitize_query,
                            strip_jurisdiction, broaden_vertical,
                            search_with_fallback (0-result broadening chain)
  synthesis/tools.py        search() / fetch() — DDG via ddgs (swappable)
  synthesis/taxonomy/seeds.yaml   Archetype risk seeds (food_service,
                            dental_clinic) — floor, not ceiling
  warmstart/                Log mining → draft rules (never auto-applies)
    logs.py                 JSONL ingest + problem-signal detection
    embed.py                local (hashed TF) / openai embedders
    cluster.py              Greedy centroid clustering (min_size >= 2)
    classify.py             Cluster → RuleProposal (llm | heuristic, +
                            direct-LLM group_and_classify_llm route)
    cache.py                Content-addressed JSON cache (.warmstart_cache/)
    pipeline.py             warm_start() / warm_start_sync() orchestration
    __main__.py             Nightly CLI: python -m gc_forge.warmstart
  redteam/probe.py          run_redteam(): LLM paraphrase/jailbreak probes
                            vs high-severity approved rules → blocked |
                            leaked | false_positive; leaks set needs_review
ui/
  Home.py                   Business + context input; Automatic vs Controlled
  _shared.py                Session state, model sidebar, tier_rules(),
                            apply_review_actions()
  pages/1_Review.py         Three tiers (standard / decide / write reply) →
                            Commit (bump + save to policies/)
  pages/2_Chat.py           Demo chat with input+output enforcement, sidebar
                            shows last fired rule
  pages/3_Warm_Start.py     Mine logs → inspect clusters/diff → send drafts
                            to Review
policies/                   Committed contracts (food_service.v1.yaml,
                            hyundai_dealership.v2.yaml)
config/models.yaml          Per-role provider:model routing
scripts/run_redteam.py      CLI wrapper for redteam/probe.py
sample_logs/                Example JSONL chat logs for warm-start
tests/                      Offline pytest suite (41 tests)
synthesis_brain.md          Orchestrator system prompt (loaded at runtime)
AGENT_SPEC.md               Same prompt, human-readable copy — keep in sync
                            with synthesis_brain.md behaviour changes
```

## The contract (do not redesign)

`Policy`: `business_type`, `intent?`, `version`, `knowledge_version`,
`input_rules[]`, `output_rules[]`.
`Rule`: `id`, `side` (input/output), `bucket`, `intent` (free text — the
engine matches on it, see below), `message?`, `route_to?`, `severity`,
`rationale`, `source`, `pii_delegate?`, `needs_review`, `review_status`.

Closed enums: `Bucket` = block / steer / graceful_reject / pass_through;
`Side` = input / output; `Severity` = low / medium / high;
`ReviewStatus` = draft / approved / edited / rejected.

Bucket semantics: `block` = hard wall (abuse, jailbreak, PII-in-text);
`steer` = in-domain but must-not-answer here (set `route_to`, e.g.
`human_handoff`); `graceful_reject` = outside intent, polite decline;
`pass_through` = allowed, no action.

## How the engine matches (important when writing rules or engine code)

`enforce(policy, text, side)` in `engine/runtime.py`:

1. Normalizes text (NFKC + zero-width strip + whitespace collapse) to defeat
   fullwidth/invisible-character evasion.
2. Considers only rules for that `side`, with matching `rule.side`, and
   `review_status` in {approved, edited}.
3. Orders by severity (high first), then hardest bucket
   (block > steer > graceful_reject), then rule id.
4. Matching per rule, in order:
   - If `pii_delegate` is set: fires **only** via Presidio (aliases mapped,
     e.g. `EMAIL`→`EMAIL_ADDRESS`; silently no-match if Presidio/spaCy
     unavailable).
   - `block` rules whose intent mentions abuse/jailbreak: hardcoded jailbreak
     regex fast-path.
   - Otherwise keyword matching on `intent`: quoted phrases (`"money back"`)
     match literally and win immediately; else ≥2 stemmed keyword hits
     (word-boundary aware — `rate` must not match `grateful`).
5. Returns a `Decision(bucket, message, route_to, matched_rule_id)` or the
   `PASS` sentinel. `pass_through` rules never produce a decision.

**Writing a good rule intent:** include explicit quoted trigger phrases from
real user wording (e.g. `detect user requests for "refund" "money back"`).
Vague intents will not fire in this dumb engine — that is by design.

Direct library use is the default (zero infra):

```python
from gc_forge.engine import runtime as gc
policy = gc.load("policies/food_service.v1.yaml")   # once at startup
d = gc.enforce(policy, user_msg, side="input")      # before the LLM
if d is not gc.PASS:
    return d.message
```

The FastAPI sidecar (`engine/service.py`, `POST /enforce`, `GET /healthz`,
`POST /reload`) is an optional thin wrapper — never duplicate engine logic
into it.

## Workflows

### Cold start (synthesis)
`synthesize(business_type, context)` in `synthesis/agent.py` (async, 3-attempt
retry). Builds a per-call Pydantic AI agent so `GC_MODEL_ORCHESTRATOR`
overrides apply without restart. The search tool sanitizes queries
(brand/city stripped) and broadens on zero results. Controlled mode
(`synthesis/controlled.py`) splits this into plan → human-approved search →
draft, and is what `ui/Home.py` "Controlled" mode drives.

### Review (HITL)
`ui/pages/1_Review.py` groups rules via `tier_rules()` in `ui/_shared.py`:
standard protections (block/PII, pre-checked), decide-the-action (bucket
judgment calls), write-the-reply (steer/graceful_reject wording). Commit
applies `apply_review_actions()`, drops `rejected`, `bump()`s the version,
and saves via `store.default_policy_path()`.

### Warm-start (log evolution)
`warmstart/pipeline.py`: JSONL turns → problem events (signals:
`assistant_deflected`, `explicitly_flagged`, `empty_or_thin_reply`,
`user_pushed_back`) → grouping → one draft `Rule` per recurring cluster
(`source=log:<slug>-<date>`, `needs_review=True`). Backends: `local`
(hashed-TF, offline default), `openai` (needs `OPENAI_API_KEY`), `llm`
(single structured call groups + classifies, falls back to local on failure).
Classifier: `llm` (falls back to heuristic) or `heuristic` (offline).
Clusters already handled by the committed contract are reported, not
duplicated. `_ensure_enforceable()` guarantees a message/route and anchors
intents with quoted phrases until coverage ≥60% of the cluster's own
examples. Results are cached in `.warmstart_cache/` (content-addressed);
`--no-cache` disables. CLI: `python -m gc_forge.warmstart --policy ... --logs ...`.

### Red-team
`redteam/probe.py: run_redteam()` probes every high-severity approved/edited
rule (LLM probes, deterministic fallback set), scores via the real engine,
sets `needs_review=True` on leaking rules, and returns the flagged policy.
Caveat: a probe that merely *mentions* PII contains no PII — a silent
Presidio rule there is correct, not a leak.

## Setup and commands

Python 3.12 (pinned in `.python-version`), managed with `uv`. Dependencies
in `pyproject.toml` (Pydantic v2, Pydantic AI, Streamlit, FastAPI, Presidio,
ddgs, Logfire).

```bash
uv sync --extra dev                     # install
cp .env.example .env                    # OLLAMA_API_KEY / GROQ_API_KEY / LOGFIRE_TOKEN
uv run pytest -q                        # 41 tests, fully offline, no keys needed
uv run streamlit run ui/Home.py         # full authoring UI
GC_CONTRACT_PATH=policies/food_service.v1.yaml uv run gc-engine   # optional sidecar
uv run python scripts/run_redteam.py policies/food_service.v1.yaml
uv run python -m gc_forge.warmstart --policy policies/food_service.v1.yaml \
    --logs sample_logs/food_service_chats.jsonl --backend local --classifier llm
```

Optional for real PII delegation: install the spaCy model Presidio expects
(`en_core_web_sm` works; the engine falls back to it automatically).

## Model routing

Per-role routing in `config/models.yaml`, resolved by `config/models.py`
(`get_model(role)`). Roles: `orchestrator`, `synthesis`, `sub_agent`
(warm-start), `redteam`, `chat`. Override per role without editing files:

```bash
export GC_MODEL_SYNTHESIS=ollama:gpt-oss:120b
export GC_MODEL_CHAT=groq:llama-3.3-70b-versatile
```

Format is `provider:model` (providers: `ollama`, `groq`). The Streamlit
sidebar (`render_model_sidebar()` in `ui/_shared.py`) sets these in-process.
Never hardcode a model or provider in a module — always go through
`get_model(ModelRole.<role>)`.

## Testing

Tests live in `tests/` and run offline (no API keys, no network):

| File | Covers |
|---|---|
| `test_engine.py` | Enforce semantics, draft inertness, store roundtrip/diff, PII-delegation-only firing, quote/apostrophe parsing, word-boundary matching, unicode/zero-width evasion, Presidio aliases, block-beats-steer ordering, side-mismatch protection |
| `test_warmstart.py` | Log parsing/signals, local embedding + clustering, heuristic bucketing, end-to-end pipeline, cache hits, cached-OpenAI skips network |
| `test_queries.py` | Banned-term extraction, sanitization, jurisdiction handling, zero-result broadening chain |
| `test_controlled.py` | Controlled-mode query running, findings rendering, auto-path isolation |

Add tests in the matching file when you change behaviour in that area. A fix
is not done until `uv run pytest -q` passes.

## Conventions for agents

- Check `git status` before editing; leave unrelated untracked/user files
  alone. Do not commit, push, or rewrite history unless explicitly asked.
- Keep imports within the walls: modules import `contract/schema.py`; allowed
  cross-imports are engine←schema, redteam←engine, warmstart←(engine, store),
  ui←(synthesis, store, engine, warmstart, config). No cycles.
- Prefer editing the engine's callers/rules over making the engine smarter.
  If engine matching must change, cover evasion and false-positive cases in
  `tests/test_engine.py`.
- When changing synthesis behaviour, update `synthesis_brain.md` (the prompt
  actually loaded) — and keep `AGENT_SPEC.md` consistent with it.
- Keep `needs_review` semantics: `False` only for clear-harm `block` rules
  and PII-delegated rules; `True` for any bucket/wording judgment call.
- Every rule needs a `rationale` and a traceable `source`
  (`taxonomy:...@v`, `web:<domain>`, `log:<cluster>`).
- Line length 100, Ruff config in `pyproject.toml`, target `py312`.
  Run `uv run ruff check src tests ui` before finishing.
- Do not add dependencies lightly; the stack is deliberately minimal.
- Verify with the repo's own tests, not ad-hoc scripts alone. Report any
  test you could not run (e.g. LLM-backed flows needing API keys) as
  unverified rather than claiming success.
