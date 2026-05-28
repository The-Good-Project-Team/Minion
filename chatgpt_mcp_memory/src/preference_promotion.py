"""Promote explicit signals into durable preference claims (and optional graph nodes)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_MIN_TEXT_LEN = 8
_AUTO_ACTIVE_CONFIDENCE = 0.85
_PROPOSED_CONFIDENCE = 0.55


def record_explicit_preference(
    conn,
    *,
    text: str,
    source: str,
    evidence_refs: Optional[List[str]] = None,
    auto_activate: bool = False,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """User-typed preference or onboarding answer → identity claim."""
    from identity import propose_identity_update

    body = (text or "").strip()
    if len(body) < _MIN_TEXT_LEN:
        return {"ok": False, "error": "text_too_short"}

    proposed, err = propose_identity_update(
        conn,
        kind="preference",
        text=body[:4000],
        source_agent=source[:64],
        confidence=_AUTO_ACTIVE_CONFIDENCE if auto_activate else _PROPOSED_CONFIDENCE,
        evidence_chunk_ids=evidence_refs or [],
        meta={**(meta or {}), "promotion": "explicit", "recorded_at": time.time()},
    )
    if err:
        return {"ok": False, "error": err}
    claim = (proposed or {}).get("claim") if isinstance(proposed, dict) else proposed

    if auto_activate and claim:
        from identity import set_claim_status

        cid = str(claim.get("claim_id") or "")
        ok, aerr = set_claim_status(conn, cid, status="active")
        if aerr or not ok:
            return {"ok": True, "claim": claim, "activated": False, "activate_error": aerr}
        from store import identity_claim_get

        claim = identity_claim_get(conn, cid) or claim
        return {"ok": True, "claim": claim, "activated": True}

    return {"ok": True, "claim": claim, "activated": False}


def record_display_name(
    conn,
    *,
    display_name: str,
    source: str = "onboarding",
) -> Dict[str, Any]:
    """Persist what Minion should call the user (identity preference claim)."""
    name = (display_name or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "display_name_too_short"}
    text = f"User prefers to be called {name}."
    return record_explicit_preference(
        conn,
        text=text,
        source=source,
        auto_activate=True,
        meta={"preference_key": "display_name", "display_name": name},
    )


def record_council_feedback(
    conn,
    *,
    proposal_type: str,
    action: str,
    title: str,
    summary: str = "",
) -> Dict[str, Any]:
    """Council approve/reject/snooze → preference signal when user is training taste."""
    action_l = (action or "").strip().lower()
    if action_l not in ("approve", "reject", "snooze"):
        return {"ok": False, "skipped": True, "reason": "not_a_taste_signal"}

    verb = {"approve": "prefers", "reject": "does not want", "snooze": "paused"}[action_l]
    text = f"User {verb} council suggestion: {title}. {summary}".strip()
    return record_explicit_preference(
        conn,
        text=text,
        source="council",
        auto_activate=action_l == "approve",
        meta={"proposal_type": proposal_type, "council_action": action_l},
    )


def record_graph_answer(
    conn,
    *,
    gap: Dict[str, Any],
    answer_text: str,
) -> Dict[str, Any]:
    """Graph-fill answer on a preference-shaped gap."""
    gtype = str(gap.get("gap_type") or "")
    node_kind = str(gap.get("node_kind") or gap.get("kind") or "")
    if node_kind != "preference" and gtype not in ("bucket", "claim"):
        return {"ok": False, "skipped": True}

    label = str(gap.get("label") or gap.get("bucket_label") or "preference")
    text = f"{label}: {(answer_text or '').strip()}"
    out = record_explicit_preference(conn, text=text, source="graph_fill", auto_activate=True)
    if out.get("ok"):
        _maybe_graph_preference_node(conn, label=label, answer=answer_text, claim_id=(out.get("claim") or {}).get("claim_id"))
    return out


def _maybe_graph_preference_node(
    conn,
    *,
    label: str,
    answer: str,
    claim_id: Optional[str],
) -> Optional[str]:
    try:
        from graph_fill import _create_graph_node

        title = (label or "Preference")[:120]
        note = (answer or "")[:500]
        nid = _create_graph_node(conn, "scaffold-preferences", "preference", title, user_note=note)
        if claim_id and nid:
            row = conn.execute("SELECT summary FROM graph_nodes WHERE node_id=?", (nid,)).fetchone()
            meta: Dict[str, Any] = {}
            if row and row["summary"]:
                try:
                    meta = json.loads(row["summary"])
                except json.JSONDecodeError:
                    pass
            meta["identity_claim_id"] = claim_id
            conn.execute(
                "UPDATE graph_nodes SET summary=?, updated_at=? WHERE node_id=?",
                (json.dumps(meta, ensure_ascii=False), time.time(), nid),
            )
        return nid
    except Exception:
        log.debug("graph preference node skipped", exc_info=True)
        return None


def list_promoted_preferences(conn, *, limit: int = 20) -> List[Dict[str, Any]]:
    from store import identity_claim_list

    return [
        c
        for c in identity_claim_list(conn, status="active", limit=limit * 2)
        if str(c.get("kind") or "") == "preference"
    ][:limit]
