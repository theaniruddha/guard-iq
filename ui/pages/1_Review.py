import streamlit as st

from _shared import apply_review_actions, init_state, render_model_sidebar, tier_rules
from gc_forge.contract.schema import Bucket, ReviewStatus
from gc_forge.store.versioning import bump, default_policy_path, save

st.set_page_config(page_title="Review", page_icon="📝", layout="wide")
init_state()
render_model_sidebar()

policy = st.session_state.policy
if policy is None:
    st.warning("No draft policy. Synthesize one on Home first.")
    st.stop()

st.title("Review draft contract")
st.caption(f"{policy.business_type} · v{policy.version} · {policy.knowledge_version}")
st.caption(f"Context: {policy.intent or '(broad)'}")

all_rules = policy.input_rules + policy.output_rules
standard, decide, reply = tier_rules(all_rules)

_warm = [r for r in all_rules if r.review_status == ReviewStatus.draft and r.source.startswith("log:")]
if _warm:
    st.info(f"🔁 {len(_warm)} draft rule(s) proposed from chat logs (warm start). "
            "They're marked 🆕 below and stay inert until you approve and commit.")

accepted: dict[str, bool] = {}
buckets: dict[str, str] = {}
messages: dict[str, str] = {}
killed: dict[str, bool] = {}


_SEVERITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_BUCKET_ICON = {
    "block": "⛔",
    "steer": "↪️",
    "graceful_reject": "🙅",
    "pass_through": "✅",
}


def render_rule_row(rule, expanded: bool = False) -> None:
    sev = _SEVERITY_ICON.get(rule.severity.value, "⚪")
    bkt = _BUCKET_ICON.get(rule.bucket.value, "")
    from_logs = rule.review_status.value == "draft" and rule.source.startswith("log:")
    badge = "🆕 from logs · " if from_logs else ""
    intent_excerpt = rule.intent if len(rule.intent) <= 70 else rule.intent[:67] + "..."
    title = f"{badge}{sev} {bkt} `{rule.id}` · {rule.side.value} · {intent_excerpt}"
    with st.expander(title, expanded=expanded):
        st.markdown(f"**Intent:** {rule.intent}")
        st.caption(f"**Rationale:** {rule.rationale}")
        st.caption(f"**Source:** {rule.source}  ·  **Severity:** {rule.severity.value}")
        if rule.pii_delegate:
            st.caption(f"**PII delegate:** `{rule.pii_delegate}`")

        col_kill, col_accept = st.columns(2)
        killed[rule.id] = col_kill.checkbox("🗑 Kill rule", key=f"kill_{rule.id}")
        if not killed[rule.id]:
            accepted[rule.id] = col_accept.checkbox("Accept", value=True, key=f"accept_{rule.id}")
            buckets[rule.id] = st.selectbox(
                "Bucket",
                [b.value for b in Bucket],
                index=[b.value for b in Bucket].index(rule.bucket.value),
                key=f"bucket_{rule.id}",
            )
            if buckets[rule.id] in (Bucket.steer.value, Bucket.graceful_reject.value):
                messages[rule.id] = st.text_area(
                    "Reply message (the business's voice)",
                    value=rule.message or "",
                    key=f"msg_{rule.id}",
                )


st.subheader(f"Standard protections ({len(standard)})")
st.caption("Block rules and PII delegation — pre-checked; untick to remove.")
for rule in standard:
    render_rule_row(rule, expanded=False)

st.subheader(f"Decide the action ({len(decide)})")
st.caption("Judgment calls — confirm or change the bucket.")
for rule in decide:
    render_rule_row(rule, expanded=True)

st.subheader(f"Write the reply ({len(reply)})")
st.caption("Steer and graceful_reject wording — this is what your customers read.")
for rule in reply:
    render_rule_row(rule, expanded=True)

# ── Commit summary ────────────────────────────────────────────────────────────
n_killed = sum(1 for v in killed.values() if v)
n_kept = len(all_rules) - n_killed
st.divider()
st.caption(f"**On commit:** {n_kept} rules kept · {n_killed} killed · version bumps to v{policy.version + 1}")

if st.button(f"Commit v{policy.version + 1}", type="primary"):
    reviewed = apply_review_actions(policy, accepted, buckets, messages, killed)
    for rules in (reviewed.input_rules, reviewed.output_rules):
        rules[:] = [r for r in rules if r.review_status != ReviewStatus.rejected]

    committed = bump(reviewed)
    path = default_policy_path(committed.business_type, committed.version)
    save(committed, path)
    st.session_state.policy = committed
    st.session_state.committed_path = str(path)
    st.success(f"Committed {path.name} (v{committed.version})")