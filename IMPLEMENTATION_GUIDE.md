# Guard-IQ Implementation Guide

Turn a business type into a **reviewed, versioned guardrail contract** (YAML),
enforce it deterministically on every chat turn, evolve it from real chat logs —
always through human review, never auto-applied.

```
 Synthesis (LLM, slow, authoring-time)          Warm-start (logs, nightly)
        │ drafts Policy                                │ drafts diff
        ▼                                              ▼
 ┌──────────────────────── Review UI (HITL — the ONLY path to commit) ───┐
 │ approve / edit / kill each rule · write the reply wording · Commit    │
 └──────────────────────────────┬────────────────────────────────────────┘
                                ▼  policies/<type>.vN.yaml
                    Engine (dumb, fast, offline)
                    gc.enforce(policy, text, side)     ◄── Red-team probes
```

---

## 1. Setup

```bash
uv sync                                   # installs everything from pyproject
# optional but recommended — activates PII delegation (Presidio):
uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

`.env` keys (see `.env.example`):

| Key | Used for |
|---|---|
| `GROQ_API_KEY` | synthesis, warm-start LLM routes, red-team, demo chat |
| `OLLAMA_CLOUD_API_KEY` / `OLLAMA_BASE_URL` | alternative provider |
| `OPENAI_API_KEY` | only if you pick the OpenAI embedding backend in warm-start |
| `LOGFIRE_TOKEN` | optional telemetry |

Model routing lives in `config/models.yaml` (per-role `provider:model`), with
env overrides `GC_MODEL_<ROLE>=provider:model` and a live switcher in the UI
sidebar. Roles: `orchestrator`, `synthesis`, `sub_agent` (warm-start), `redteam`, `chat`.

Run everything:

```bash
uv run streamlit run ui/Home.py           # the whole authoring loop
uv run pytest -q                          # 41 tests, all offline
```

---

## 2. The contract (single source of truth)

`src/gc_forge/contract/schema.py` — `Policy` holding `input_rules` / `output_rules`
of `Rule`. Closed enums: `side` (input/output), `bucket`
(block / steer / graceful_reject / pass_through), `severity`, `review_status`
(draft / approved / edited / rejected). Open text: `intent`, `message`,
`rationale`, `source`. `pii_delegate` (e.g. `presidio:EMAIL,PHONE`) is the only
PII mechanism — the engine never detects PII itself.

**The engine enforces only `approved`/`edited` rules.** Drafts are inert by
construction, which is what makes warm-start safe.

`store/versioning.py` gives `save` / `load` / `bump` / `diff` — the diff is
rule-level (add / remove / modify) and powers the warm-start review flow.

---

## 3. Authoring a contract (cold start)

**UI:** Home → enter business type (+ optional context) → Synthesize → Review → Commit.

- *Automatic mode:* the Pydantic-AI orchestrator (system prompt:
  `synthesis_brain.md`, seeds: `synthesis/taxonomy/seeds.yaml`) researches the
  domain via DDG search and returns a validated `Policy` of draft rules.
- *Controlled mode:* the agent first proposes its search queries; you
  approve/edit/skip/add them; searches run as plain Python; the draft is built
  only from the findings you approved.

**Review** renders three tiers — standard protections (block/PII, pre-checked),
"decide the action" (bucket judgment calls), "write the reply" (steer/
graceful_reject wording — the business's voice). Commit drops rejected rules,
bumps the version, writes `policies/<type>.vN.yaml`.

**Chat** loads a committed contract and demos both-side enforcement with the
firing rule shown in the sidebar.

---

## 4. Enforcing (production)

### Direct library (default — zero infra)

```python
from gc_forge.engine import runtime as gc

policy = gc.load("policies/food_service.v2.yaml")        # once at startup

d = gc.enforce(policy, user_msg, side="input")            # before the LLM
if d is not gc.PASS:
    return d.message                                      # block/steer/reject reply

draft = call_your_llm(user_msg)
d = gc.enforce(policy, draft, side="output")              # before sending
return d.message if d is not gc.PASS else draft
```

Decision fields: `bucket`, `message`, `route_to` (e.g. `human_handoff`),
`matched_rule_id`. Pure function of (contract, text): no network, no LLM.

What the engine does per call: normalizes the text (NFKC + zero-width strip —
defeats `ＩＧＮＯＲＥ`-style and invisible-character evasion), orders rules by
severity then hardest-bucket-first, and matches via quoted phrases from the
rule's `intent` (literal), stemmed keywords (≥2 hits), hardcoded jailbreak
patterns (block rules), or Presidio (only for `pii_delegate` rules; common
aliases like `EMAIL`→`EMAIL_ADDRESS` are mapped; degrades gracefully if
Presidio is absent).

### Optional sidecar (Presidio-style service)

```bash
GC_CONTRACT_PATH=policies/food_service.v2.yaml uv run gc-engine
# POST /enforce {"text": "...", "side": "input"} -> decision
# GET  /healthz · POST /reload?path=...
```

Thin FastAPI wrapper over the same runtime — no duplicated logic.

---

## 5. Warm-start (evolve from chat logs, HITL)

Mines logs for **recurring problems** — the same unmet ask showing up again and
again, deflections, flagged answers, users demanding a human — then:
groups occurrences → classifies the intent behind each group → maps it to a
bucket → drafts one rule per group → surfaces everything as a **diff into
Review**. Never auto-applies; drafts are ignored by the engine until a human
commits them.

### Log format (JSONL, one turn per line)

```json
{"conversation_id": "c01", "role": "user", "text": "How many loyalty points do I have?"}
{"conversation_id": "c01", "role": "assistant", "text": "You have 500 points!", "feedback": "negative"}
```

Optional per-turn: `ts`, `feedback: "negative"`, `flag: "bad_response"`.
Problem signals detected: `assistant_deflected`, `explicitly_flagged`,
`empty_or_thin_reply`, `user_pushed_back`. See `sample_logs/food_service_chats.jsonl`.

### Three parsing routes (pick per run)

| Backend | How it groups | Needs |
|---|---|---|
| `local` (default) | hashed TF vectors + greedy cosine clustering — offline, free, deterministic | nothing |
| `openai` | OpenAI embedding API, same clustering — better across paraphrases | `OPENAI_API_KEY` |
| `llm` | no embeddings: one structured LLM call groups **and** classifies (catches "rewards balance" ≈ "loyalty points") | Groq/Ollama key |

For `local`/`openai`, the intent→bucket classifier is `llm` (default; falls
back to heuristic on any failure) or `heuristic` (fully offline keyword
mapping — fine for nightly jobs without keys).

Clusters already covered by the committed contract (the engine fires on their
examples) are reported as **already handled** and produce no duplicate rule.

### Run it

UI: **Warm Start** page → pick contract + logs + route → Mine logs → inspect
clusters & diff → "Send drafts to Review" → normal Review flow → Commit.

Nightly job (cron):

```bash
uv run python -m gc_forge.warmstart \
  --policy policies/food_service.v1.yaml \
  --logs /var/log/bot/chats.jsonl \
  --backend local --classifier llm
# writes policies/food_service.v1.candidate.yaml + prints the cluster report/diff
# a human loads the candidate in Review the next morning
```

### Caching (re-runs are cheap)

`.warmstart_cache/` holds content-addressed JSON: embeddings keyed by
`sha256(backend|model|text)`, LLM groupings/classifications keyed by the
cluster's content hash. Unchanged logs ⇒ zero re-embedding and zero LLM calls
(`cache_stats` in every result shows hits/misses). `--no-cache` disables.

---

## 6. Red-team

```bash
uv run python scripts/run_redteam.py policies/food_service.v1.yaml
```

For every **high-severity approved** rule, an LLM generates paraphrase +
jailbreak probes (with a deterministic fallback set if structured output
fails); each probe runs through the real engine and is scored
`blocked | leaked | false_positive`. Leaking rules get `needs_review=True` and
the flagged policy is written to `<policy>.redteam.yaml` — load it in Review,
tighten the rule (usually: add the leaked phrasing as a quoted phrase in
`intent`), re-commit, re-probe.

Interpretation caveat: a probe that merely *talks about* PII contains no PII,
so a silent Presidio rule on it is correct behavior, not a leak; read the
probe text before reacting to the score.

---

## 7. Testing

`uv run pytest -q` — fully offline (no API keys needed):

- `tests/test_engine.py` — enforcement semantics, draft-rule inertness,
  store roundtrip/diff, PII-delegation-only firing, quote/apostrophe parsing,
  word-boundary matching, unicode/zero-width evasion, Presidio entity aliases,
  block-beats-steer tie ordering, side-mismatch protection.
- `tests/test_warmstart.py` — log parsing, signal detection, clustering,
  heuristic bucketing, full pipeline (drafts + already-handled + diff),
  cache-hit behavior, cached-OpenAI-skips-network.

---

## 8. Operational invariants (don't break these)

1. **Only Review commits.** Synthesis and warm-start output drafts; the engine
   ignores drafts; every committed version is a human decision.
2. **The engine stays dumb.** No LLM, no network, no live system state at
   enforcement time. Smarter matching belongs in better *rules*, not engine ML.
3. **PII is always delegated.** Rules say `pii_delegate: presidio:...`; the
   system never grows its own PII detection.
4. **Contracts are versioned files.** `policies/<type>.vN.yaml`; evolution is a
   reviewed diff (`store.diff`), warm-start candidates live beside them as
   `*.candidate.yaml` and never overwrite a committed version.
5. **Synthesis sees a business type, never a company name.**
