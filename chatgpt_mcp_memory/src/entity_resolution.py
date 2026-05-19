"""Resolve contacts/calendar evidence into graph person nodes."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from store import _new_id


def ensure_person_node(
    conn,
    *,
    label: str,
    external_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """Find or create a person under People scaffold."""
    parent_id = "scaffold-people-friends"
    if external_id:
        rows = conn.execute(
            "SELECT node_id, summary FROM graph_nodes WHERE node_kind='person' "
            "AND status NOT IN ('scaffold', 'stub')"
        ).fetchall()
        for row in rows:
            m = _parse_meta_row(row)
            if m.get("external_id") == external_id:
                return str(row["node_id"])
    row = conn.execute(
        "SELECT node_id FROM graph_nodes WHERE node_kind='person' AND title=? "
        "AND parent_node_id=? LIMIT 1",
        (label[:200], parent_id),
    ).fetchone()
    if row:
        return str(row["node_id"])
    nid = _new_id("gn")
    meta_out = dict(meta or {})
    if external_id:
        meta_out["external_id"] = external_id
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, ?, '[]', ?, 0.5, '[]', "
        "'vault_local', ?, ?)",
        (
            nid,
            label[:200],
            parent_id,
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
        if c.get("birthday"):
            meta["birthday"] = c["birthday"]
        pid = ensure_person_node(conn, label=label, external_id=str(ext) if ext else None, meta=meta)
        link_belongs_to_scaffold(conn, pid)
        count += 1
    return count


def _parse_meta_row(row: Any) -> Dict[str, Any]:
    try:
        return json.loads(row["summary"] or "{}")
    except json.JSONDecodeError:
        return {}
