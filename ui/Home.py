import asyncio

import streamlit as st

from _shared import init_state, render_model_sidebar
from gc_forge.synthesis.agent import synthesize
from gc_forge.synthesis.controlled import (
    draft_from_findings,
    fetch_deeper,
    plan_research,
    run_queries,
)

st.set_page_config(page_title="Guard-iq Forge", page_icon="🛡️", layout="wide")
init_state()
render_model_sidebar()

# Controlled-mode session state (kept separate from the shared defaults).
for _key, _default in (("research_plan", None), ("findings", None)):
    if _key not in st.session_state:
        st.session_state[_key] = _default

st.title("Guardrail Contract Forge")
st.caption("Turn a business (+ optional context) into a reviewed, versioned guardrail contract. The orchestrator researches risks using web search then proposes a draft for you to review.")

business_type = st.text_input("Business", placeholder="pizza restaurant, dental clinic, ...")
context = st.text_area(
    "Context (what the bot should do, or leave blank for broad)",
    placeholder="E.g. handle menu questions and order status. Never offer refunds or medical advice.",
    height=100,
)

mode = st.radio(
    "Synthesis mode",
    ["Automatic", "Controlled"],
    horizontal=True,
    help="Automatic: the orchestrator researches and drafts on its own. "
    "Controlled: it first proposes the search queries so you can approve, edit, "
    "skip, or add queries before any rules are drafted.",
)

# ── Automatic (default, unchanged behaviour) ─────────────────────────────────

if mode == "Automatic":
    if st.button("Synthesize", type="primary", disabled=not business_type.strip()):
        bt = business_type.strip()
        ctx = context.strip() or None

        with st.status("Synthesizing guardrail contract...", expanded=True) as status:
            status.write("**Step 1/4** — Loading risk taxonomy seeds (health, legal, financial, etc.)")
            status.write("**Step 2/4** — Building research prompt from your business type + context")
            status.write("**Step 3/4** — Calling orchestrator (LLM + web search via DDG). This usually takes 20–90 seconds.")

            try:
                policy = asyncio.run(synthesize(bt, ctx))
                status.write("**Step 4/4** — Validating Policy structure and attaching metadata")
                status.update(label="✅ Draft contract ready!", state="complete")

                st.session_state.policy = policy
                st.success(f"Draft ready — {len(policy.input_rules)} input / {len(policy.output_rules)} output rules.")
                st.switch_page("pages/1_Review.py")
            except Exception as exc:
                status.update(label="Synthesis failed", state="error")
                st.error(f"Synthesis failed: {exc}")

                # Helpful debugging hint
                st.info(
                    "Tips:\n"
                    "- Make sure your GROQ_API_KEY (or OLLAMA keys) are set in .env\n"
                    "- Try overriding the model: `export GC_MODEL_ORCHESTRATOR=groq:llama-3.3-70b-versatile`\n"
                    "- Check terminal logs for the exact error."
                )

# ── Controlled (opt-in, human gates the research) ────────────────────────────

else:
    # Step 1 — propose the research plan
    if st.button("Propose research plan", type="primary", disabled=not business_type.strip()):
        st.session_state.findings = None
        with st.spinner("Asking the orchestrator which searches it wants to run..."):
            try:
                plan = asyncio.run(plan_research(business_type.strip(), context.strip() or None))
                st.session_state.research_plan = [
                    {"query": q.query, "reason": q.reason, "run": True} for q in plan.queries
                ]
            except Exception as exc:
                st.error(f"Planning failed: {exc}")

    # Step 2 — human gates the plan: approve / edit / skip / add
    if st.session_state.research_plan is not None:
        st.subheader("Research plan — approve, edit, skip, or add")
        st.caption("These searches shape which rules get proposed. Untick to skip; edit the text freely.")

        plan_rows = st.session_state.research_plan
        for i, row in enumerate(plan_rows):
            cols = st.columns([0.08, 0.52, 0.4])
            row["run"] = cols[0].checkbox("run", value=row["run"], key=f"plan_run_{i}", label_visibility="collapsed")
            row["query"] = cols[1].text_input("query", value=row["query"], key=f"plan_q_{i}", label_visibility="collapsed")
            cols[2].caption(row["reason"])

        new_query = st.text_input("Add your own query", key="plan_new_q", placeholder="e.g. Ontario dealership advertising rules")
        if st.button("＋ Add query", disabled=not new_query.strip()):
            plan_rows.append({"query": new_query.strip(), "reason": "added by reviewer", "run": True})
            st.rerun()

        approved = [r["query"] for r in plan_rows if r["run"] and r["query"].strip()]
        st.caption(f"{len(approved)} of {len(plan_rows)} queries approved.")

        if st.button("Run approved searches", type="primary", disabled=not approved):
            with st.spinner(f"Running {len(approved)} searches (DDG)..."):
                st.session_state.findings = run_queries(approved)

    # Step 3 — show findings, then draft
    if st.session_state.findings is not None:
        st.subheader("Findings")
        st.caption("Snippets go into the draft. If one looks thin, fetch the full page — fetched content is included too.")
        for fi, finding in enumerate(st.session_state.findings):
            with st.expander(f"🔍 {finding.query} — {len(finding.results)} results"):
                if finding.fallback_stage:
                    st.caption(
                        f"⤷ 0 results as phrased; retried broader "
                        f"({finding.fallback_stage.replace('_', ' ')}): "
                        f"**{finding.effective_query}**"
                    )
                for ri, r in enumerate(finding.results):
                    cols = st.columns([0.85, 0.15])
                    cols[0].markdown(f"**[{r.get('title', '(no title)')}]({r.get('url', '')})**")
                    if r.get("snippet"):
                        cols[0].caption(r["snippet"])
                    if r.get("content"):
                        cols[1].caption("📄 fetched")
                    elif r.get("url") and cols[1].button("📄 Fetch", key=f"fetch_{fi}_{ri}"):
                        with st.spinner("Fetching page..."):
                            fetch_deeper(finding, r["url"])
                        st.rerun()

        if st.button("Draft rules from these findings", type="primary"):
            with st.status("Drafting guardrail contract from approved research...", expanded=True) as status:
                try:
                    policy = asyncio.run(
                        draft_from_findings(
                            business_type.strip(),
                            context.strip() or None,
                            st.session_state.findings,
                        )
                    )
                    status.update(label="✅ Draft contract ready!", state="complete")
                    st.session_state.policy = policy
                    st.session_state.research_plan = None
                    st.session_state.findings = None
                    st.success(f"Draft ready — {len(policy.input_rules)} input / {len(policy.output_rules)} output rules.")
                    st.switch_page("pages/1_Review.py")
                except Exception as exc:
                    status.update(label="Drafting failed", state="error")
                    st.error(f"Drafting failed: {exc}")

if st.session_state.policy:
    st.info("A draft policy is in session. Open Review to edit and commit.")
