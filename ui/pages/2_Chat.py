import asyncio
from pathlib import Path

import streamlit as st
from pydantic_ai import Agent

from _shared import init_state, render_model_sidebar
from gc_forge.config.models import ModelRole, get_model
from gc_forge.engine import runtime as gc
from gc_forge.store.versioning import load

st.set_page_config(page_title="Chat", page_icon="💬", layout="wide")
init_state()
render_model_sidebar()

policies_dir = Path(__file__).resolve().parents[2] / "policies"
policy_files = sorted(policies_dir.glob("*.yaml")) if policies_dir.exists() else []

if not policy_files:
    st.warning("No committed policies in policies/. Commit one in Review first.")
    st.stop()

labels = [p.name for p in policy_files]
selected = st.selectbox("Committed contract", labels)
policy = load(policies_dir / selected)
st.session_state.chat_policy = policy
st.caption(f"{policy.business_type} · v{policy.version} · context: {policy.intent or 'broad'}")

if st.session_state.get("chat_agent_for") != selected:
    st.session_state.chat_agent_for = selected
    st.session_state.chat_messages = []
    st.session_state.chat_agent = Agent(
        get_model(ModelRole.chat),
        system_prompt=(
            f"You are a helpful assistant for a {policy.business_type} business. "
            f"Context: {policy.intent or 'general support'}. "
            "Be concise and never guarantee prices, refunds, or medical/allergen claims."
        ),
    )

for msg in st.session_state.chat_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("decision"):
            st.caption(f"Guardrail: {msg['decision']}")

if user_msg := st.chat_input("Say something…"):
    st.session_state.chat_messages.append({"role": "user", "content": user_msg})
    input_decision = gc.enforce(policy, user_msg, side="input")

    if input_decision is not gc.PASS:
        reply = input_decision.message or "Blocked by guardrail."
        st.session_state.chat_messages.append(
            {
                "role": "assistant",
                "content": reply,
                "decision": f"{input_decision.bucket.value} · {input_decision.matched_rule_id}",
            }
        )
        st.session_state.last_decision = input_decision
        st.rerun()

    with st.spinner("Thinking…"):
        result = asyncio.run(st.session_state.chat_agent.run(user_msg))
        draft = str(result.output)

    output_decision = gc.enforce(policy, draft, side="output")
    if output_decision is not gc.PASS:
        reply = output_decision.message or "Response blocked by guardrail."
        decision_note = f"{output_decision.bucket.value} · {output_decision.matched_rule_id}"
    else:
        reply = draft
        decision_note = None

    st.session_state.chat_messages.append(
        {"role": "assistant", "content": reply, "decision": decision_note}
    )
    st.session_state.last_decision = output_decision if output_decision is not gc.PASS else None
    st.rerun()

if st.session_state.last_decision:
    d = st.session_state.last_decision
    st.sidebar.markdown("**Last fired rule**")
    st.sidebar.write(d.matched_rule_id)
    st.sidebar.write(d.bucket.value)