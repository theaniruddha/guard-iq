"""Warm-start: mine chat logs for recurring unhandled problems, propose draft
rules, review them in the same Review surface. Nothing is auto-applied."""

from pathlib import Path

import streamlit as st

from _shared import init_state, render_model_sidebar
from gc_forge.store.versioning import load
from gc_forge.warmstart.pipeline import warm_start_sync

st.set_page_config(page_title="Warm Start", page_icon="🔁", layout="wide")
init_state()
render_model_sidebar()

if "warmstart_result" not in st.session_state:
    st.session_state.warmstart_result = None

ROOT = Path(__file__).resolve().parents[2]
policies_dir = ROOT / "policies"

st.title("Warm start from chat logs")
st.caption(
    "Mines logs for the same unmet ask recurring — misroutes, deflections, bad answers — "
    "groups them, classifies the intent, maps it to a bucket, and drafts one rule per group. "
    "Drafts land in Review; the engine ignores them until a human commits."
)

policy_files = sorted(p for p in policies_dir.glob("*.yaml") if "candidate" not in p.name)
if not policy_files:
    st.warning("No committed policies in policies/. Commit one first.")
    st.stop()

col1, col2 = st.columns(2)
selected = col1.selectbox("Committed contract", [p.name for p in policy_files])

default_logs = ROOT / "sample_logs" / "food_service_chats.jsonl"
logs_path = col2.text_input("Chat logs (JSONL path)", value=str(default_logs))
uploaded = st.file_uploader("…or upload a JSONL file", type=["jsonl", "json"])

st.markdown("**Parsing route** — how logs are grouped into topics")
backend = st.radio(
    "backend",
    ["local", "openai", "llm"],
    horizontal=True,
    label_visibility="collapsed",
    format_func=lambda b: {
        "local": "Local embeddings (offline, free)",
        "openai": "OpenAI embeddings (needs OPENAI_API_KEY)",
        "llm": "Direct LLM (no embeddings — one call groups + classifies)",
    }[b],
)
classifier = "llm"
if backend != "llm":
    classifier = st.radio(
        "Intent → bucket classifier",
        ["llm", "heuristic"],
        horizontal=True,
        format_func=lambda c: "LLM (better labels, falls back to heuristic)" if c == "llm" else "Heuristic (offline)",
    )
use_cache = st.checkbox("Use cache (skip re-embedding / re-classifying unchanged logs)", value=True)

if st.button("Mine logs", type="primary"):
    if uploaded is not None:
        tmp = ROOT / ".warmstart_cache" / "uploaded.jsonl"
        tmp.parent.mkdir(exist_ok=True)
        tmp.write_bytes(uploaded.getvalue())
        logs_path = str(tmp)
    try:
        policy = load(policies_dir / selected)
        with st.spinner("Mining logs → clustering → classifying…"):
            st.session_state.warmstart_result = warm_start_sync(
                policy, logs_path, backend=backend, classifier=classifier, use_cache=use_cache
            )
    except Exception as exc:
        st.error(f"Warm start failed: {exc}")

result = st.session_state.warmstart_result
if result is None:
    st.stop()

st.divider()
st.subheader("What the logs say")
st.caption(
    f"{result.n_turns} turns → {result.n_events} problem events → {len(result.clusters)} recurring groups · "
    f"cache: {result.cache_stats}"
)
if result.fallback_used:
    st.warning("Direct-LLM grouping failed on this run — fell back to local embeddings + LLM classifier.")

new_rules = 0
for c in result.clusters:
    if c.already_handled_by:
        icon, verdict = "✅", f"already handled by `{c.already_handled_by}` — nothing to add"
    else:
        icon, verdict = "🆕", f"drafted rule `{c.drafted_rule_id}`"
        new_rules += 1
    with st.expander(f"{icon} **{c.topic_slug}** · {c.size} occurrences · {verdict}", expanded=not c.already_handled_by):
        st.caption("Signals: " + ", ".join(c.signals))
        for ex in c.examples:
            st.markdown(f'> "{ex}"')
        if c.proposal and not c.already_handled_by:
            p = c.proposal
            st.markdown(
                f"**Proposed:** `{p.bucket.value}` on `{p.side.value}` · severity {p.severity.value}"
                + (f" · route → {p.route_to}" if p.route_to else "")
                + (f" · matches {c.coverage:.0%} of its own examples" if c.coverage is not None else "")
            )
            st.markdown(f"**Intent:** {p.intent_label}")
            if p.message:
                st.markdown(f"**Reply:** {p.message}")
            st.caption(f"Rationale: {p.rationale}")

st.divider()
st.subheader(f"Diff vs committed contract ({len([ch for ch in result.changes if ch.get('change') == 'add'])} additions)")
for ch in result.changes:
    if ch.get("change") == "add":
        rule = ch["rule"]
        st.markdown(f"➕ `{ch['rule_id']}` ({ch['side']}) — {rule.intent}")
    elif ch.get("change") == "knowledge_version":
        st.caption(f"knowledge_version: {ch['old']} → {ch['new']}")

if new_rules:
    st.info("Drafts are inert until reviewed — the engine only enforces approved/edited rules.")
    if st.button(f"Send {new_rules} draft rule(s) to Review", type="primary"):
        st.session_state.policy = result.candidate
        st.switch_page("pages/1_Review.py")
else:
    st.success("No gaps found — every recurring problem is already covered by the contract.")
