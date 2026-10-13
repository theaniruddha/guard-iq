from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gc_forge.config.models import ModelRole, load_model_config, model_label  # noqa: E402
from gc_forge.config.telemetry import configure_logfire  # noqa: E402
from gc_forge.contract.schema import Bucket, Policy, ReviewStatus, Rule, Severity  # noqa: E402

configure_logfire("guard-iq-ui")


def init_state() -> None:
    defaults = {
        "policy": None,
        "committed_path": None,
        "chat_policy": None,
        "chat_messages": [],
        "last_decision": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# Known-good provider:model options for the sidebar switcher.
# Free text is also accepted (env override format: provider:model).
_MODEL_CHOICES = [
    "ollama:gpt-oss:120b",
    "ollama:gpt-oss:20b",
    "ollama:minimax-m3",
    "ollama:glm-5.2",
    "ollama:nemotron-3-super",
    "groq:llama-3.3-70b-versatile",
]


def render_model_sidebar() -> None:
    """Per-role model switcher. Selecting a model sets the GC_MODEL_<ROLE>
    env override in-process — get_model() reads it on every call, so the
    change applies to the next synthesis/chat run without restart."""
    import os

    st.sidebar.subheader("Model routing")
    config = load_model_config()
    for role_name, role_cfg in config.roles.items():
        current = model_label(role_name)
        options = list(dict.fromkeys([current, *_MODEL_CHOICES]))
        chosen = st.sidebar.selectbox(
            role_name,
            options,
            index=0,
            key=f"model_{role_name}",
            help=role_cfg.description or None,
        )
        env_key = f"GC_MODEL_{role_name.upper()}"
        if chosen != f"{role_cfg.provider}:{role_cfg.model}":
            os.environ[env_key] = chosen
        else:
            os.environ.pop(env_key, None)


def tier_rules(rules: list[Rule]) -> tuple[list[Rule], list[Rule], list[Rule]]:
    standard: list[Rule] = []
    decide: list[Rule] = []
    reply: list[Rule] = []
    for rule in rules:
        if rule.bucket == Bucket.block or rule.pii_delegate:
            standard.append(rule)
        elif rule.needs_review and rule.severity in (Severity.medium, Severity.high):
            decide.append(rule)
        elif rule.bucket in (Bucket.steer, Bucket.graceful_reject):
            reply.append(rule)
        else:
            decide.append(rule)
    return standard, decide, reply


def apply_review_actions(
    policy: Policy,
    accepted: dict[str, bool],
    buckets: dict[str, str],
    messages: dict[str, str],
    killed: dict[str, bool],
) -> Policy:
    updated = policy.model_copy(deep=True)
    for rules in (updated.input_rules, updated.output_rules):
        for rule in rules:
            if killed.get(rule.id):
                rule.review_status = ReviewStatus.rejected
                continue
            if not accepted.get(rule.id, True):
                continue
            changed = False
            if rule.id in buckets and rule.bucket != Bucket(buckets[rule.id]):
                rule.bucket = Bucket(buckets[rule.id])
                changed = True
            if rule.id in messages and (rule.message or "") != messages[rule.id]:
                rule.message = messages[rule.id]
                changed = True
            if rule.review_status == ReviewStatus.draft:
                rule.review_status = ReviewStatus.edited if changed else ReviewStatus.approved
            elif changed:
                rule.review_status = ReviewStatus.edited
            rule.needs_review = False
    return updated