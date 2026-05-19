"""Extensible skill registry for council proposal execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

ExecuteResult = Dict[str, Any]
ExecuteFn = Callable[[Dict[str, Any], Dict[str, Any]], ExecuteResult]


@dataclass(frozen=True)
class SkillSpec:
    skill_id: str
    required_info_keys: Tuple[str, ...]
    approval_labels: Dict[str, str]
    intensity_default: str = "standard"
    execute: Optional[ExecuteFn] = None


_REGISTRY: Dict[str, SkillSpec] = {}


def register_skill(spec: SkillSpec) -> None:
    _REGISTRY[spec.skill_id] = spec


def get_skill(skill_id: str) -> Optional[SkillSpec]:
    return _REGISTRY.get(skill_id)


def list_skills() -> List[SkillSpec]:
    return list(_REGISTRY.values())


def approval_options_for_skill(skill_id: str) -> List[Dict[str, str]]:
    spec = get_skill(skill_id)
    labels = (spec.approval_labels if spec else {}) or {}
    default = {
        "approve": "Approve",
        "edit": "Edit",
        "snooze": "Snooze",
        "reject": "Dismiss",
    }
    merged = {**default, **labels}
    order = ("approve", "edit", "snooze", "reject")
    return [{"id": k, "label": merged[k]} for k in order if k in merged]


def _stub_execute(proposal: Dict[str, Any], _caps: Dict[str, Any]) -> ExecuteResult:
    return {
        "ok": True,
        "stub": True,
        "proposal_id": proposal.get("proposal_id"),
        "message": "Skill execution stubbed; use desktop bridge for native action.",
    }


def _register_defaults() -> None:
    register_skill(
        SkillSpec(
            skill_id="send_message",
            required_info_keys=("recipient_channel", "consent_outbound"),
            approval_labels={"approve": "Send?", "reject": "Dismiss"},
            intensity_default="standard",
            execute=_stub_execute,
        )
    )
    register_skill(
        SkillSpec(
            skill_id="execute_purchase",
            required_info_keys=(
                "payment_method",
                "delivery_place",
                "consent_commerce",
            ),
            approval_labels={"approve": "Yes", "reject": "No"},
            intensity_default="elevated",
            execute=_stub_execute,
        )
    )
    register_skill(
        SkillSpec(
            skill_id="create_calendar_hold",
            required_info_keys=("calendar_access", "consent_calendar"),
            approval_labels={"approve": "Add?", "reject": "Dismiss"},
            intensity_default="standard",
            execute=_stub_execute,
        )
    )


_register_defaults()
