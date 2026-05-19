"""HTTP handlers for council approval pipeline."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from council_learning import apply_approval_learning
from council_skills import get_skill
from council_store import (
    council_approval_insert,
    council_proposal_get,
    council_proposal_set_status,
)


def handle_council_approve(
    conn,
    *,
    proposal_id: str,
    action: str,
    edited_payload: Optional[Dict[str, Any]] = None,
    snooze_days: Optional[float] = None,
) -> Dict[str, Any]:
    prop = council_proposal_get(conn, proposal_id)
    if not prop:
        return {"ok": False, "error": "proposal_not_found"}
    if prop.get("status") != "open":
        return {"ok": False, "error": "proposal_not_open"}

    action = (action or "").strip().lower()
    if action == "edit" and edited_payload:
        action = "approve"

    payload = prop["payload"]
    if edited_payload:
        payload = {**payload, **edited_payload}

    council_approval_insert(
        conn,
        proposal_id=proposal_id,
        action=action,
        snooze_until=time.time() + float(snooze_days or 7) * 86400.0 if action == "snooze" else None,
        edited_payload=edited_payload,
    )

    apply_approval_learning(
        conn,
        pattern_id=prop["pattern_id"],
        subject_id=prop["subject_id"],
        action=action,
        snooze_days=snooze_days,
        proposal_created_at=prop.get("created_at"),
    )

    execute_result: Optional[Dict[str, Any]] = None
    if action == "approve":
        skill = get_skill(prop["required_skill"])
        if skill and skill.execute:
            execute_result = skill.execute({**prop, "payload": payload}, {})
        council_proposal_set_status(conn, proposal_id, "approved", payload=payload)
        _telemetry("council_approved", proposal_id, prop.get("pattern_id"))
    elif action in ("reject", "dismiss"):
        council_proposal_set_status(conn, proposal_id, "rejected")
        _telemetry("council_rejected", proposal_id, prop.get("pattern_id"))
    elif action == "snooze":
        council_proposal_set_status(conn, proposal_id, "snoozed")
    else:
        council_proposal_set_status(conn, proposal_id, "closed")

    conn.commit()
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "action": action,
        "execute": execute_result,
    }


def _telemetry(kind: str, proposal_id: str, pattern_id: Optional[str]) -> None:
    try:
        from telemetry import log_event

        log_event(kind, proposal_id=proposal_id, pattern_id=pattern_id)
    except Exception:
        pass
