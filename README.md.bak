# Guard-iq — Guardrail Contract Forge

Turn a **business type** (+ optional **intent**) into a reviewed, versioned guardrail contract (YAML), then enforce it on chat input and output.

## Stack

- **Synthesis / red-team**: Pydantic AI + Logfire
- **Models**: Ollama Cloud (Nemotron Super/Nano) + Groq — per-role routing in `config/models.yaml`
- **Engine**: deterministic library (`gc_forge.engine.runtime`) + optional FastAPI sidecar
- **UI**: Streamlit (Home → Review → Chat)

## Setup (Python 3.12 via uv)

```bash
cd guard-iq
uv sync --extra dev
cp .env.example .env   # add OLLAMA_API_KEY, GROQ_API_KEY, LOGFIRE_TOKEN
```

## Model routing

Edit `config/models.yaml` or override per role:

```bash
export GC_MODEL_SYNTHESIS=ollama:nemotron-3-super:cloud
export GC_MODEL_CHAT=groq:llama-3.3-70b-versatile
```

| Role | Default | Use |
|------|---------|-----|
| orchestrator / synthesis | `groq:llama-3.3-70b-versatile` (default) | Research + draft Policy |
| sub_agent / redteam | `ollama:nemotron-3-nano:cloud` | Probes, light tasks |
| chat | `groq:llama-3.3-70b-versatile` | Demo chatbot |

## Run

```bash
# Streamlit UI
uv run streamlit run ui/Home.py

# Engine sidecar (optional)
GC_CONTRACT_PATH=policies/food_service.v1.yaml uv run gc-engine

# Tests
uv run pytest
```

## Layout

```
src/gc_forge/
  contract/     # Policy + Rule schema
  config/       # Model routing + Logfire
  store/        # YAML save/load/diff
  engine/       # enforce() + FastAPI service
  synthesis/    # Pydantic AI agent + search tools
  redteam/      # Probe high-severity rules
ui/             # Streamlit multipage app
policies/       # Committed contracts
config/models.yaml
```