"""Persistence for the extensible council pipeline (events, proposals, learning)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from store import _new_id


def council_event_insert(
    conn,
    *,
    event_type: str,
    domain: str,
    subject_id: str,
    pattern_id: str,
    evidence_refs: Optional[List[str]] = None,
    detected_at: Optional[float] = None,
) -> str:
    eid = _new_id("cev")
    now = float(detected_at if detected_at is not None else time.time())
    conn.execute(
        "INSERT INTO council_events(event_id, event_type, domain, subject_id, pattern_id, "
        "evidence_refs_json, detected_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            eid,
            event_type[:64],
            domain[:32],
            subject_id[:256],
            pattern_id[:64],
            json.dumps(evidence_refs or [], ensure_ascii=False),
            now,
        ),
    )
    return eid


def council_proposal_upsert_open(
    conn,
    *,
    event_id: str,
    pattern_id: str,
    subject_id: str,
    proposal_type: str,
    title: str,
    summary: str,
    payload: Dict[str, Any],
    intensity: str,
    required_skill: str,
    required_info: Dict[str, Any],
) -> str:
    """One open proposal per (pattern_id, subject_id); refresh if exists."""
    row = conn.execute(
        "SELECT proposal_id FROM council_proposals WHERE pattern_id=? AND subject_id=? "
        "AND status='open' LIMIT 1",
        (pattern_id, subject_id),
    ).fetchone()
    now = time.time()
    if row:
        pid = str(row["proposal_id"])
        conn.execute(
            "UPDATE council_proposals SET event_id=?, proposal_type=?, title=?, summary=?, "
            "payload_json=?, intensity=?, required_skill=?, required_info_json=?, updated_at=? "
            "WHERE proposal_id=?",
            (
                event_id,
                proposal_type[:64],
                title[:500],
                summary[:2000],
                json.dumps(payload, ensure_ascii=False),
                intensity[:32],
                required_skill[:64],
                json.dumps(required_info, ensure_ascii=False),
                now,
                pid,
            ),
        )
        return pid
    pid = _new_id("cprop")
    conn.execute(
        "INSERT INTO council_proposals(proposal_id, event_id, proposal_type, title, summary, "
        "payload_json, intensity, required_skill, required_info_json, status, pattern_id, "
        "subject_id, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
        (
            pid,
            event_id,
            proposal_type[:64],
            title[:500],
            summary[:2000],
            json.dumps(payload, ensure_ascii=False),
            intensity[:32],
            required_skill[:64],
            json.dumps(required_info, ensure_ascii=False),
            pattern_id[:64],
            subject_id[:256],
            now,
            now,
        ),
    )
    return pid


def council_proposal_get(conn, proposal_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT proposal_id, event_id, proposal_type, title, summary, payload_json, intensity, "
        "required_skill, required_info_json, status, pattern_id, subject_id, created_at, updated_at "
        "FROM council_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    return _row_proposal(row) if row else None


def council_proposals_list_open(
    conn, *, limit: int = 20, intensity: Optional[str] = None
) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 100)))
    clauses = ["status='open'"]
    params: List[Any] = []
    if intensity:
        clauses.append("intensity=?")
        params.append(intensity[:32])
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT proposal_id, event_id, proposal_type, title, summary, payload_json, intensity, "
        f"required_skill, required_info_json, status, pattern_id, subject_id, created_at, updated_at "
        f"FROM council_proposals WHERE {where} "
        f"ORDER BY CASE intensity WHEN 'elevated' THEN 0 ELSE 1 END, updated_at DESC LIMIT ?",
        (*params, lim),
    ).fetchall()
    return [_row_proposal(r) for r in rows]


def council_proposal_set_status(
    conn, proposal_id: str, status: str, *, payload: Optional[Dict[str, Any]] = None
) -> bool:
    now = time.time()
    if payload is not None:
        cur = conn.execute(
            "UPDATE council_proposals SET status=?, payload_json=?, updated_at=? WHERE proposal_id=?",
            (status[:32], json.dumps(payload, ensure_ascii=False), now, proposal_id),
        )
    else:
        cur = conn.execute(
            "UPDATE council_proposals SET status=?, updated_at=? WHERE proposal_id=?",
            (status[:32], now, proposal_id),
        )
    return cur.rowcount > 0


def council_proposals_expire_stale(conn, *, older_than_sec: float = 86400.0 * 14) -> int:
    cutoff = time.time() - older_than_sec
    cur = conn.execute(
        "UPDATE council_proposals SET status='expired', updated_at=? "
        "WHERE status='open' AND updated_at < ?",
        (time.time(), cutoff),
    )
    return cur.rowcount


def council_approval_insert(
    conn,
    *,
    proposal_id: str,
    action: str,
    snooze_until: Optional[float] = None,
    edited_payload: Optional[Dict[str, Any]] = None,
) -> str:
    aid = _new_id("cappr")
    conn.execute(
        "INSERT INTO council_approvals(approval_id, proposal_id, action, snooze_until, "
        "edited_payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
        (
            aid,
            proposal_id,
            action[:32],
            float(snooze_until) if snooze_until is not None else None,
            json.dumps(edited_payload, ensure_ascii=False) if edited_payload else None,
            time.time(),
        ),
    )
    return aid


def council_pattern_state_get(
    conn, pattern_id: str, subject_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT pattern_id, subject_id, learned_cadence_days, suppress_until, reject_count, "
        "last_approved_at, last_signal_at, meta_json FROM council_pattern_state "
        "WHERE pattern_id=? AND subject_id=?",
        (pattern_id, subject_id),
    ).fetchone()
    return _row_pattern_state(row) if row else None


def council_pattern_state_upsert(
    conn,
    *,
    pattern_id: str,
    subject_id: str,
    learned_cadence_days: Optional[float] = None,
    suppress_until: Optional[float] = None,
    reject_count: Optional[int] = None,
    last_approved_at: Optional[float] = None,
    last_signal_at: Optional[float] = None,
    meta_merge: Optional[Dict[str, Any]] = None,
) -> None:
    existing = council_pattern_state_get(conn, pattern_id, subject_id)
    if existing:
        lcd = (
            learned_cadence_days
            if learned_cadence_days is not None
            else existing.get("learned_cadence_days")
        )
        sup = suppress_until if suppress_until is not None else existing.get("suppress_until")
        rc = reject_count if reject_count is not None else existing.get("reject_count", 0)
        la = last_approved_at if last_approved_at is not None else existing.get("last_approved_at")
        ls = last_signal_at if last_signal_at is not None else existing.get("last_signal_at")
        meta = dict(existing.get("meta") or {})
        if meta_merge:
            meta.update(meta_merge)
        conn.execute(
            "UPDATE council_pattern_state SET learned_cadence_days=?, suppress_until=?, "
            "reject_count=?, last_approved_at=?, last_signal_at=?, meta_json=? "
            "WHERE pattern_id=? AND subject_id=?",
            (
                lcd,
                sup,
                int(rc),
                la,
                ls,
                json.dumps(meta, ensure_ascii=False),
                pattern_id,
                subject_id,
            ),
        )
        return
    meta: Dict[str, Any] = meta_merge or {}
    conn.execute(
        "INSERT INTO council_pattern_state(pattern_id, subject_id, learned_cadence_days, "
        "suppress_until, reject_count, last_approved_at, last_signal_at, meta_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pattern_id,
            subject_id,
            learned_cadence_days,
            suppress_until,
            int(reject_count or 0),
            last_approved_at,
            last_signal_at,
            json.dumps(meta, ensure_ascii=False),
        ),
    )


def council_pattern_state_is_suppressed(
    conn, pattern_id: str, subject_id: str, *, now: Optional[float] = None
) -> bool:
    st = council_pattern_state_get(conn, pattern_id, subject_id)
    if not st:
        return False
    until = st.get("suppress_until")
    if until is None:
        return False
    return float(until) > float(now if now is not None else time.time())


def capability_ref_list(conn, *, cap_key: Optional[str] = None) -> List[Dict[str, Any]]:
    if cap_key:
        rows = conn.execute(
            "SELECT ref_id, cap_key, provider, label, vault_ref, status, meta_json, created_at, updated_at "
            "FROM capability_refs WHERE cap_key=? AND status='active'",
            (cap_key,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ref_id, cap_key, provider, label, vault_ref, status, meta_json, created_at, updated_at "
            "FROM capability_refs WHERE status='active'"
        ).fetchall()
    return [_row_cap(r) for r in rows]


def capability_ref_upsert(
    conn,
    *,
    cap_key: str,
    label: str,
    vault_ref: str,
    provider: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    row = conn.execute(
        "SELECT ref_id FROM capability_refs WHERE cap_key=? AND status='active' LIMIT 1",
        (cap_key,),
    ).fetchone()
    now = time.time()
    if row:
        rid = str(row["ref_id"])
        conn.execute(
            "UPDATE capability_refs SET label=?, vault_ref=?, provider=?, meta_json=?, updated_at=? "
            "WHERE ref_id=?",
            (
                label[:200],
                vault_ref[:512],
                provider[:64],
                json.dumps(meta or {}, ensure_ascii=False),
                now,
                rid,
            ),
        )
        return rid
    rid = _new_id("cap")
    conn.execute(
        "INSERT INTO capability_refs(ref_id, cap_key, provider, label, vault_ref, status, "
        "meta_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (
            rid,
            cap_key[:64],
            provider[:64],
            label[:200],
            vault_ref[:512],
            json.dumps(meta or {}, ensure_ascii=False),
            now,
            now,
        ),
    )
    return rid


def council_open_subject_ids(conn) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT subject_id FROM council_proposals WHERE status='open'"
    ).fetchall()
    return {str(r["subject_id"]) for r in rows}


def _row_proposal(row: Any) -> Dict[str, Any]:
    return {
        "proposal_id": row["proposal_id"],
        "event_id": row["event_id"],
        "proposal_type": row["proposal_type"],
        "title": row["title"],
        "summary": row["summary"],
        "payload": json.loads(row["payload_json"] or "{}"),
        "intensity": row["intensity"],
        "required_skill": row["required_skill"],
        "required_info": json.loads(row["required_info_json"] or "{}"),
        "status": row["status"],
        "pattern_id": row["pattern_id"],
        "subject_id": row["subject_id"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _row_pattern_state(row: Any) -> Dict[str, Any]:
    return {
        "pattern_id": row["pattern_id"],
        "subject_id": row["subject_id"],
        "learned_cadence_days": float(row["learned_cadence_days"])
        if row["learned_cadence_days"] is not None
        else None,
        "suppress_until": float(row["suppress_until"]) if row["suppress_until"] is not None else None,
        "reject_count": int(row["reject_count"] or 0),
        "last_approved_at": float(row["last_approved_at"])
        if row["last_approved_at"] is not None
        else None,
        "last_signal_at": float(row["last_signal_at"]) if row["last_signal_at"] is not None else None,
        "meta": json.loads(row["meta_json"] or "{}"),
    }


def _row_cap(row: Any) -> Dict[str, Any]:
    return {
        "ref_id": row["ref_id"],
        "cap_key": row["cap_key"],
        "provider": row["provider"],
        "label": row["label"],
        "vault_ref": row["vault_ref"],
        "status": row["status"],
        "meta": json.loads(row["meta_json"] or "{}"),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }
