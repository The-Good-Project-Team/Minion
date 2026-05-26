"""Resolve contacts/calendar evidence into graph person nodes."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from store import _new_id, graph_candidate_create


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _norm_identifier(s: Any) -> str:
    raw = str(s or "").strip().casefold()
    if "@" in raw:
        return raw
    digits = re.sub(r"\D+", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits or raw


def ensure_person_node(
    conn,
    *,
    label: str,
    external_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """Find or create a person under People scaffold."""
    from graph_fill import _is_plausible_graph_title

    if not _is_plausible_graph_title(label, node_kind="person"):
        raise ValueError(f"implausible person label: {label!r}")
    parent_id = "scaffold-people-friends"
    meta_in = dict(meta or {})
    identifiers = _person_identifiers(external_id=external_id, meta=meta_in)
    rows = conn.execute(
        "SELECT node_id, title, aliases_json, summary FROM graph_nodes WHERE node_kind='person' "
        "AND status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    for row in rows:
        if _row_matches_person(row, label=label, identifiers=identifiers):
            return str(row["node_id"])

    row = conn.execute(
        "SELECT node_id, title, aliases_json, summary FROM graph_nodes WHERE node_kind='person' AND title=? "
        "AND parent_node_id=? LIMIT 1",
        (label[:200], parent_id),
    ).fetchone()
    if row:
        existing_ids = _person_identifiers(meta=_parse_meta_row(row))
        if identifiers and existing_ids and not identifiers.intersection(existing_ids):
            reasoning = _merge_candidate_reasoning(
                existing_label=str(row["title"] or ""),
                incoming_label=label,
                existing_identifiers=existing_ids,
                incoming_identifiers=identifiers,
            )
            graph_candidate_create(
                conn,
                candidate_type="person_merge",
                title=f"Is {label[:120]} the same person?",
                body_md=(
                    "A local source used the same name but a different identifier.\n\n"
                    + "\n".join(f"- {r}" for r in reasoning)
                ),
                payload={
                    "existing_node_id": str(row["node_id"]),
                    "incoming_label": label[:200],
                    "incoming_identifiers": sorted(identifiers),
                    "existing_identifiers": sorted(existing_ids),
                    "incoming_meta": meta_in,
                    "reasoning": reasoning,
                    "resolution_policy": "shared identifiers merge automatically; same-name identifier conflicts require confirmation",
                },
                evidence_refs=_evidence_refs_from_meta(meta_in),
                confidence=0.62,
                source=str(meta_in.get("source") or "entity_resolution"),
            )
        return str(row["node_id"])
    nid = _new_id("gn")
    meta_out = meta_in
    if external_id:
        meta_out["external_id"] = external_id
    aliases = sorted(
        {
            a
            for a in [
                str(meta_out.get("handle") or ""),
                str(meta_out.get("imessage") or ""),
                str(meta_out.get("email") or ""),
            ]
            if a.strip()
        }
    )
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, ?, ?, ?, 0.5, '[]', "
        "'vault_local', ?, ?)",
        (
            nid,
            label[:200],
            parent_id,
            json.dumps(aliases, ensure_ascii=False),
            json.dumps(meta_out, ensure_ascii=False),
            now,
            now,
        ),
    )
    return nid


def link_belongs_to_scaffold(conn, person_id: str, parent_node_id: str = "scaffold-people") -> None:
    exists = conn.execute(
        "SELECT 1 FROM graph_edges WHERE from_node_id=? AND to_node_id=? AND rel_kind='belongs_to'",
        (person_id, parent_node_id),
    ).fetchone()
    if exists:
        return
    eid = _new_id("ge")
    now = time.time()
    conn.execute(
        "INSERT INTO graph_edges(edge_id, from_node_id, to_node_id, rel_kind, created_at, updated_at) "
        "VALUES(?, ?, ?, 'belongs_to', ?, ?)",
        (eid, person_id, parent_node_id, now, now),
    )


def ingest_contacts_snapshot(conn, contacts: List[Dict[str, Any]]) -> int:
    count = 0
    for c in contacts:
        label = (c.get("display_name") or c.get("name") or "").strip()
        if not label:
            continue
        ext = c.get("id") or c.get("identifier")
        meta: Dict[str, Any] = {}
        if c.get("phone"):
            meta["phone"] = c["phone"]
        if c.get("email"):
            meta["email"] = c["email"]
        if c.get("handle") or c.get("imessage"):
            meta["handle"] = c.get("handle") or c.get("imessage")
            meta["imessage"] = c.get("imessage") or c.get("handle")
        if c.get("birthday"):
            meta["birthday"] = c["birthday"]
        if c.get("source"):
            meta["source"] = c["source"]
        pid = ensure_person_node(conn, label=label, external_id=str(ext) if ext else None, meta=meta)
        link_belongs_to_scaffold(conn, pid)
        count += 1
    return count


def _parse_meta_row(row: Any) -> Dict[str, Any]:
    try:
        return json.loads(row["summary"] or "{}")
    except json.JSONDecodeError:
        return {}


def _person_identifiers(
    *,
    external_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> set[str]:
    m = meta or {}
    out: set[str] = set()
    for key in ("external_id", "phone", "email", "handle", "imessage", "identifier"):
        v = external_id if key == "external_id" and external_id else m.get(key)
        if v:
            norm = _norm_identifier(v)
            if norm:
                out.add(f"{key}:{norm}" if key == "external_id" else norm)
    return out


def _row_matches_person(row: Any, *, label: str, identifiers: set[str]) -> bool:
    meta = _parse_meta_row(row)
    row_ids = _person_identifiers(meta=meta)
    if identifiers and row_ids.intersection(identifiers):
        return True
    label_norm = _norm_text(label)
    if identifiers and row_ids and not identifiers.intersection(row_ids):
        return False
    if label_norm and label_norm == _norm_text(str(row["title"] or "")):
        return True
    try:
        aliases = json.loads(row["aliases_json"] or "[]")
    except Exception:
        aliases = []
    return any(label_norm == _norm_text(str(a)) for a in aliases if str(a).strip())


def _evidence_refs_from_meta(meta: Dict[str, Any]) -> List[str]:
    refs = meta.get("evidence_refs") or meta.get("source_refs") or []
    if isinstance(refs, list):
        return [str(r) for r in refs if str(r).strip()][:8]
    if isinstance(refs, str) and refs.strip():
        return [refs.strip()]
    return []


def _merge_candidate_reasoning(
    *,
    existing_label: str,
    incoming_label: str,
    existing_identifiers: set[str],
    incoming_identifiers: set[str],
) -> List[str]:
    out = []
    if _norm_text(existing_label) == _norm_text(incoming_label):
        out.append(f"Name matches existing graph person `{existing_label}`.")
    else:
        out.append(f"Incoming label `{incoming_label}` is close to existing graph person `{existing_label}`.")
    out.append(f"Existing identifiers: {', '.join(sorted(existing_identifiers))}.")
    out.append(f"Incoming identifiers: {', '.join(sorted(incoming_identifiers))}.")
    out.append("No shared hard identifier was found, so Minion is asking before merging.")
    return out
