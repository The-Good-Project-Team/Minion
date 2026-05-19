"""Resolve required_info keys for council gating."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from council_store import capability_ref_list
from council_skills import get_skill

InfoEntry = Dict[str, Any]


def resolve_required_info(
    conn,
    keys: Tuple[str, ...],
    *,
    subject_id: str,
    data_dir: Optional[Any] = None,
) -> Dict[str, InfoEntry]:
    out: Dict[str, InfoEntry] = {}
    for key in keys:
        out[key] = _resolve_one(conn, key, subject_id=subject_id, data_dir=data_dir)
    return out


def gate_intensity(
    declared_intensity: str,
    required_info: Dict[str, InfoEntry],
    skill_id: str,
) -> Tuple[str, bool]:
    """Return (effective_intensity, should_surface)."""
    spec = get_skill(skill_id)
    keys = spec.required_info_keys if spec else tuple(required_info.keys())
    if not keys:
        return declared_intensity, True
    statuses = [required_info.get(k, {}).get("status", "missing") for k in keys]
    if all(s == "ready" for s in statuses):
        return declared_intensity, True
    consent_keys = [k for k in keys if k.startswith("consent_")]
    if declared_intensity == "elevated":
        if any(required_info.get(k, {}).get("status") != "ready" for k in consent_keys):
            return declared_intensity, False
        if any(s != "ready" for s in statuses):
            return "standard", True
    if any(s == "ready" for s in statuses):
        return "standard", True
    return declared_intensity, False


def _resolve_one(
    conn, key: str, *, subject_id: str, data_dir: Optional[Any] = None
) -> InfoEntry:
    if key == "payment_method":
        caps = capability_ref_list(conn, cap_key="payment_method")
        if caps:
            c = caps[0]
            vault = str(c.get("vault_ref") or "")
            if vault.startswith("keychain:") or vault:
                return {"status": "ready", "ref": c["ref_id"], "label": c["label"]}
        return {"status": "missing", "label": "No payment method on file"}
    if key == "delivery_place":
        place = _graph_place_for_subject(conn, subject_id)
        if place:
            return {"status": "ready", "ref": place, "label": place}
        return {"status": "missing"}
    if key == "recipient_channel":
        ch = _recipient_channel(conn, subject_id, data_dir)
        if ch:
            return {"status": "ready", **ch}
        return {"status": "missing"}
    if key.startswith("consent_"):
        return _consent_flag(conn, key, data_dir)
    if key == "calendar_access":
        return {"status": "ready", "label": "Calendar"}
    return {"status": "missing"}


def _consent_flag(conn, key: str, data_dir: Optional[Any]) -> InfoEntry:
    if data_dir is None:
        return {"status": "missing"}
    try:
        from settings import load_settings

        s = load_settings(data_dir)
        flags = s.get("council_consent") or {}
        if flags.get(key) is True:
            return {"status": "ready"}
    except Exception:
        pass
    default_ready = key in ("consent_outbound", "consent_calendar")
    return {"status": "ready" if default_ready else "missing"}


def _recipient_channel(
    conn, subject_id: str, data_dir: Optional[Any]
) -> Optional[Dict[str, str]]:
    row = conn.execute(
        "SELECT title, summary, body_md FROM graph_nodes WHERE node_id=?",
        (subject_id,),
    ).fetchone()
    if not row:
        return None
    meta: Dict[str, Any] = {}
    for field in ("summary", "body_md"):
        raw = row[field]
        if raw and str(raw).strip().startswith("{"):
            try:
                meta = json.loads(raw)
                break
            except json.JSONDecodeError:
                pass
    phone = meta.get("phone") or meta.get("imessage")
    if phone:
        return {"ref": str(phone), "label": str(phone)}
    label = str(row["title"] or "")
    if label:
        return {"ref": subject_id, "label": label}
    return None


def _graph_place_for_subject(conn, subject_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT n.title FROM graph_edges e JOIN graph_nodes n ON n.node_id=e.to_node_id "
        "WHERE e.from_node_id=? AND e.rel_kind='lives_at' LIMIT 1",
        (subject_id,),
    ).fetchone()
    return str(row["title"]) if row else None
