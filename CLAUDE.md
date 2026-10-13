# CLAUDE.md — Guardrail Contract Forge


## 1. What this is (and what it deliberately is not)

A small, modular system that turns a **business type** (plus an optional
**intent**) into a **reviewed, versioned guardrail contract** (YAML), and lets an
admin review/edit it and chat against it.

Four concerns, four walls:

1. **Synthesis** — an agent researches the domain's risks and drafts candidate
   rules into a fixed envelope. Runs at authoring time. Allowed to be smart/slow.
2. **Review (HITL)** — a human accepts/edits/kills each drafted rule and sets the
   reply wording. The ONLY path to a committed contract.
3. **Engine** — loads a committed contract and enforces it on input AND output.
   Dumb, fast, deterministic, offline. Knows nothing about agents or research.
   Usable two ways (§5.6): as a **direct library** (`import gc` inside your own
   agent — zero infra) or as an **optional sidecar** (a small FastAPI service
   called like Presidio). The core logic is identical; the sidecar is a thin
   wrapper.
4. **Red-team** — loads a contract, probes high-severity rules, flags leaks.

This is **NOT**: a chatbot platform, a PII detector (delegated to Presidio), or a
live self-modifying policy. Evolution happens as **human-approved diffs** to the
contract, never auto-applied. The engine never models live system state
(service-up/down, API errors, retrieval hits) — that is runtime concern, not
policy.

---

## 2. Repository structure

```
gc-forge/
├── CLAUDE.md                      # this file
├── pyproject.toml
├── src/gc_forge/
│   ├── contract/
│   │   └── schema.py              # THE interface (§3). Everything imports this.
│   ├── synthesis/
│   │   ├── agent.py              # Pydantic AI agent → returns Policy
│   │   ├── tools.py             # search(), fetch()
│   │   └── taxonomy/seeds.yaml  # per-archetype risk seeds (extendable hints)
│   ├── store/
│   │   └── versioning.py         # save / load / version-bump / diff contracts
│   ├── redteam/
│   │   └── probe.py             # load contract → probe → score → flag
│   └── engine/
│       ├── runtime.py            # gc.load(), gc.enforce()  — pure library, direct use
│       └── service.py           # OPTIONAL FastAPI sidecar wrapping runtime.py
├── ui/
│   ├── Home.py                   # entry: enter type+intent, run synthesis
│   └── pages/
│       ├── 1_Review.py          # admin review/edit surface → commit
│       └── 2_Chat.py            # load committed contract → chat with enforcement
├── policies/                     # committed YAML contracts land here (versioned)
└── synthesis_brain.md            # the synthesis agent's system prompt (loaded by agent.py)
```

---

## 3. The contract (canonical — copy verbatim, do not redesign)

This is the only interface. The agent fills the *values* freely; the *fields* are
fixed so the UI can render, the engine can run, and warm-start can diff. Put this
in `src/gc_forge/contract/schema.py` exactly:

```python
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Side(str, Enum):
    input = "input"
    output = "output"


class Bucket(str, Enum):
    block = "block"                       # hard wall: abuse, jailbreak, PII-in-text
    steer = "steer"                       # redirect / hand off to human or alt path
    graceful_reject = "graceful_reject"   # polite out-of-scope decline
    pass_through = "pass_through"         # allowed, no action


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ReviewStatus(str, Enum):
    draft = "draft"        # agent proposed, awaiting human
    approved = "approved"  # human accepted as-is
    edited = "edited"      # human modified then accepted
    rejected = "rejected"  # human killed it


class Rule(BaseModel):
    id: str
    side: Side
    bucket: Bucket
    intent: str                              # free text: behavior this rule catches
    message: Optional[str] = None            # reply to emit (None for pass_through)
    route_to: Optional[str] = None           # e.g. "human_handoff" (steer)
    severity: Severity
    rationale: str                           # why this rule exists
    source: str                              # "taxonomy:food@v2" | "web:<domain>" | "log:<cluster>"
    pii_delegate: Optional[str] = None       # e.g. "presidio:PERSON,EMAIL" — never handled here
    needs_review: bool = True
    review_status: ReviewStatus = ReviewStatus.draft


class Policy(BaseModel):
    business_type: str                       # required input
    intent: Optional[str] = None             # optional input — sets aperture & out-of-scope
    version: int = 1
    knowledge_version: str                   # taxonomy/pack version synthesized against
    input_rules: list[Rule] = Field(default_factory=list)
    output_rules: list[Rule] = Field(default_factory=list)
```

Rules for fields: `intent`, `message`, `rationale`, `source` are **open text** the
agent writes. The enums are **closed** — the agent must pick one of the four
buckets, one side, one severity. `pii_delegate` is the only PII handling allowed;
the system never detects PII itself.

---

## 4. Tech stack

| Layer | Pick | Why |
|---|---|---|
| Synthesis agent | **Pydantic AI** | typed output = the contract is the result_type; no graph control flow needed |
| Schema | **Pydantic v2** | the envelope; single source of truth |
| Serialization | **PyYAML** | contracts are YAML on disk, Policy objects in memory |
| Search tool | **DDG** (swappable) | tool interface only; agent must not assume provider |
| PII | **Presidio** (delegated) | never reimplement; agent only sets `pii_delegate` |
| UI | **Streamlit** (multipage) | editable review table + chat in one framework |
| Sidecar (optional) | **FastAPI + uvicorn** | thin wrapper over the engine; Presidio-style service. Not needed for direct/library use |
| Red-team | LLM-generated probes + engine | reuses the engine; no separate enforcement path |

Python 3.11+. Keep dependencies minimal.

---

## 5. Module specs

### 5.1 `contract/schema.py`
Just §3. No logic. Everything imports `Policy` and `Rule` from here.

### 5.2 `synthesis/agent.py`
- A Pydantic AI `Agent` with `result_type=Policy`.
- System prompt: load text from `synthesis_brain.md` (do not inline it here).
- Tools: `search`, `fetch` from `synthesis/tools.py`.
- Inputs: `business_type: str` (required), `intent: str | None` (optional).
- Behavior: seed from `taxonomy/seeds.yaml` for the matched archetype, research
  the domain (by **type, never a company name**), draft `Rule`s for **both**
  `input` and `output` sides, set `needs_review` per the tiering in
  synthesis_brain.md, return a validated `Policy`.
- Output: a `Policy` object. Do not write files here — hand the object back.

### 5.3 `synthesis/tools.py`
- `search(query) -> list[{title, url, snippet}]` (DDG; provider-swappable).
- `fetch(url) -> str` (optional deeper read).
- No business logic; thin wrappers.

### 5.4 `synthesis/taxonomy/seeds.yaml`
Per archetype, the risk **dimensions** and seed concerns — hints that sharpen the
agent's research, NOT a fixed list it is bound to. Example shape:

```yaml
food_service:
  dimensions:
    health: [allergen_claims, foodborne_illness_advice, dietary_safety_for_conditions]
    financial: [refunds, unverified_discounts, price_commitments]
    reputational: [abuse_handling, public_complaint_bait]
    legal: [allergen_disclosure, health_claim_regulation]
dental_clinic:
  dimensions:
    health: [medical_advice, diagnosis_requests, medication_safety]
    financial: [quote_commitments, insurance_claims]
    legal: [hipaa_adjacent_phi, consent]
```
The agent may add dimensions it discovers. Seeds are a floor, not a ceiling.

### 5.5 `store/versioning.py`
- `save(policy, path) -> path` — serialize Policy to YAML under `policies/`.
- `load(path) -> Policy` — deserialize and validate.
- `bump(policy) -> Policy` — increment `version`.
- `diff(old: Policy, new: Policy) -> list[change]` — rule-level add/remove/modify,
  used by warm-start to propose evolutions. Diff is possible only because both
  share the §3 shape.

### 5.6 `engine/runtime.py` — the core (pure library, direct use)
- `gc.load(path) -> Policy`.
- `gc.enforce(policy, text, side) -> Decision` where
  `Decision = {bucket, message, route_to, matched_rule_id} | PASS`.
- Enforce both sides: call once with `side="input"` on the user message before the
  LLM, once with `side="output"` on the model's draft before sending.
- Only consider rules whose `review_status` is `approved` or `edited`. Ignore
  `draft` and `rejected`.
- For `block`: return a canned safety message. For `steer`/`graceful_reject`:
  return the rule's `message` (and `route_to` if set). For `pass_through`: PASS.
- For any rule with `pii_delegate`, call Presidio for that entity set; do not
  detect PII here.
- No web calls, no agent, no live state. Pure function of (contract, text).

**Direct use (default, zero infra).** This module is a plain library. Any agent in
the same process uses it with no service:
```python
from gc_forge.engine import runtime as gc
policy = gc.load("policies/dental_clinic.v3.yaml")   # once at startup
d = gc.enforce(policy, user_msg, side="input")        # per message
if d is not PASS:
    return d.message
```
For a single small bot, this is all you need. Do not stand up a service.

### 5.6b `engine/service.py` — OPTIONAL sidecar (Presidio-style)
A thin FastAPI wrapper over `runtime.py`, for when a chatbot wants guardrails as a
network call (same pattern as Presidio). The core logic is NOT duplicated — the
service just imports `runtime` and exposes it.
- Startup: `gc.load(<contract path or env var>)` once, hold in memory.
- `POST /enforce  {text, side}  -> {decision}` — returns the same `Decision`.
- `GET  /healthz  -> {status, contract_version}`.
- Stateless per request; reload on a new committed version (endpoint or restart).
- Run: `uvicorn gc.engine.service:app`. A chatbot calls this exactly like it calls
  Presidio — and can call both: Presidio for PII, this for business rules.

Build `runtime.py` first and fully; `service.py` is a small add-on, not a rewrite.
A user who only needs the library never installs FastAPI.

### 5.7 `redteam/probe.py`
- Input: a committed `Policy`.
- For each `high` severity rule, generate paraphrase + jailbreak probes (LLM).
- Run each probe through `engine.enforce`; record `blocked | leaked | false_positive`.
- For any leaking rule, set `needs_review=True` and emit a report table.
- Output: a report object + the updated Policy (sent back through Review).

---

## 6. UI spec (Streamlit, multipage)

### `Home.py`
- Two inputs: `business_type` (text, required), `intent` (text area, optional,
  labeled "what should the bot do? leave blank for a broad sweep").
- Button "Synthesize" → calls `synthesis.agent` → stores the returned `Policy` in
  `st.session_state` → switches to Review.

### `pages/1_Review.py` — the surface that makes or breaks this
Render the Policy's rules in **three tiers** (use expanders/sections):
- **Standard protections** (auto-include): `block` rules, PII-delegated rules.
  Show collapsed, read-only-ish, pre-checked. The admin can untick but isn't
  forced to decide.
- **Decide the action**: rules where the bucket is a judgment call
  (`needs_review=True`, severity medium/high). Show the agent's proposed `bucket`
  with a selectbox to change it among the four buckets.
- **Write the reply**: any `steer` / `graceful_reject` rule. Show an editable text
  box pre-filled with the agent's `message`; this is the business's voice.
- Each rule row: accept (→ `approved`), edit (→ `edited`), kill (→ `rejected`),
  with `rationale` and `source` shown so the admin understands *why*.
- "Commit" button → drop `rejected` rules, `store.bump()`, `store.save()` to
  `policies/`. Show the new version number.

### `pages/2_Chat.py`
- Dropdown to load a committed contract from `policies/`.
- `st.chat_input` loop: run `gc.enforce(policy, user_msg, "input")`; if not PASS,
  show the rule's message and stop. Else call the LLM, run
  `gc.enforce(policy, draft, "output")`; if not PASS, show the rule's message
  instead of the draft. Else show the draft.
- Small sidebar showing which rule fired, for demo legibility.

Do not build a custom frontend. Streamlit only.

---

## 7. Build order

1. `contract/schema.py` (§3) — nothing works without the envelope.
2. `store/versioning.py` — save/load/diff against the schema.
3. `engine/runtime.py` — enforce against the schema (test with a hand-written YAML).
   The sidecar `engine/service.py` is an OPTIONAL later add-on — only after
   `runtime.py` works as a library. Skip it entirely for direct/single-agent use.
4. `synthesis/tools.py` + `synthesis/agent.py` + load `synthesis_brain.md`.
5. `synthesis/taxonomy/seeds.yaml` — one archetype (food_service) end to end first.
6. `redteam/probe.py`.
7. `ui/` — Home → Review → Chat, in that order.

Ship one archetype (food_service) through the whole loop before adding more.

---

## 8. Conventions

- Single source of truth is `contract/schema.py`. Never duplicate the rule shape.
- Modules import the schema; they do not import each other except engine←schema,
  redteam←engine, ui←(synthesis, store, engine).
- No buzzword identifiers; name things for what they do.
- Every committed contract is a YAML file under `policies/`, version in filename
  (e.g. `food_service.v3.yaml`).
- The agent never receives a company name, only a type (+ optional intent).