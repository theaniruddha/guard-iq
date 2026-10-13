from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from gc_forge.contract.schema import Policy, Rule


def _rule_to_dict(rule: Rule) -> dict[str, Any]:
    return rule.model_dump(mode="json")


def _policy_to_dict(policy: Policy) -> dict[str, Any]:
    data = policy.model_dump(mode="json")
    data["input_rules"] = [_rule_to_dict(r) for r in policy.input_rules]
    data["output_rules"] = [_rule_to_dict(r) for r in policy.output_rules]
    return data


def save(policy: Policy, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _policy_to_dict(policy)
    target.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return target


def load(path: str | Path) -> Policy:
    data = yaml.safe_load(Path(path).read_text())
    return Policy.model_validate(data)


def bump(policy: Policy) -> Policy:
    data = policy.model_dump()
    data["version"] = policy.version + 1
    return Policy.model_validate(data)


def _rule_key(rule: Rule) -> str:
    return rule.id


def diff(old: Policy, new: Policy) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []

    for side_attr in ("input_rules", "output_rules"):
        old_rules = {_rule_key(r): r for r in getattr(old, side_attr)}
        new_rules = {_rule_key(r): r for r in getattr(new, side_attr)}

        for rule_id in sorted(set(old_rules) | set(new_rules)):
            if rule_id not in old_rules:
                changes.append(
                    {"side": side_attr, "change": "add", "rule_id": rule_id, "rule": new_rules[rule_id]}
                )
            elif rule_id not in new_rules:
                changes.append(
                    {"side": side_attr, "change": "remove", "rule_id": rule_id, "rule": old_rules[rule_id]}
                )
            else:
                old_rule = old_rules[rule_id].model_dump()
                new_rule = new_rules[rule_id].model_dump()
                if old_rule != new_rule:
                    changes.append(
                        {
                            "side": side_attr,
                            "change": "modify",
                            "rule_id": rule_id,
                            "old": old_rules[rule_id],
                            "new": new_rules[rule_id],
                        }
                    )

    if old.business_type != new.business_type:
        changes.append({"change": "business_type", "old": old.business_type, "new": new.business_type})
    if old.intent != new.intent:
        changes.append({"change": "intent", "old": old.intent, "new": new.intent})
    if old.knowledge_version != new.knowledge_version:
        changes.append(
            {
                "change": "knowledge_version",
                "old": old.knowledge_version,
                "new": new.knowledge_version,
            }
        )

    return changes


def default_policy_path(business_type: str, version: int, policies_dir: str | Path = "policies") -> Path:
    slug = business_type.strip().lower().replace(" ", "_")
    return Path(policies_dir) / f"{slug}.v{version}.yaml"