"""Remove greeting/noise person nodes and stale threads tied to them."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def is_phantom_person_title(title: str) -> bool:
    from graph_fill import _is_plausible_graph_title

    label = (title or "").strip()
    if not label:
        return True
    return not _is_plausible_graph_title(label, node_kind="person")


def _phantom_label_from_proposal(title: str, summary: str = "") -> bool:
    text = f"{title} {summary}".strip()
    m = re.search(r"\bwith\s+(.+)$", text, flags=re.I)
    if m and is_phantom_person_title(m.group(1).strip()):
        return True
    return is_phantom_person_title(text)


def _remove_person_node(conn, node_id: str) -> None:
    conn.execute("DELETE FROM graph_edges WHERE from_node_id=? OR to_node_id=?", (node_id, node_id))
    conn.execute("DELETE FROM graph_nodes WHERE node_id=?", (node_id,))


def purge_phantom_graph_artifacts(conn, *, commit: bool = True) -> Dict[str, Any]:
    """Drop greeting-shaped people and close threads/proposals that reference them."""
    from chat_store import chat_thread_resolve
    from council_store import council_proposal_set_status

    removed_nodes: List[str] = []
    resolved_threads: List[str] = []
    expired_proposals: List[str] = []

    rows = conn.execute(
        "SELECT node_id, title FROM graph_nodes "
        "WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    for row in rows:
        nid = str(row["node_id"])
        title = str(row["title"] or "")
        if not is_phantom_person_title(title):
            continue
        _remove_person_node(conn, nid)
        removed_nodes.append(nid)
        log.info("removed phantom person node %s (%r)", nid, title)

    threads = conn.execute(
        "SELECT thread_id, subject_id, meta_json FROM chat_threads WHERE status='open'"
    ).fetchall()
    for row in threads:
        tid = str(row["thread_id"])
        sid = str(row["subject_id"] or "")
        meta: Dict[str, Any] = {}
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        gap = dict(meta.get("gap") or {})
        label = str(gap.get("label") or "")
        gap_sid = str(gap.get("subject_id") or sid or "")
        missing = bool(gap_sid) and not conn.execute(
            "SELECT 1 FROM graph_nodes WHERE node_id=?", (gap_sid,)
        ).fetchone()
        phantom = is_phantom_person_title(label)
        if missing or phantom:
            chat_thread_resolve(conn, tid)
            resolved_threads.append(tid)

    proposals = conn.execute(
        "SELECT proposal_id, subject_id, title, summary FROM council_proposals WHERE status='open'"
    ).fetchall()
    for row in proposals:
        pid = str(row["proposal_id"])
        sid = str(row["subject_id"] or "")
        title = str(row["title"] or "")
        summary = str(row["summary"] or "")
        missing = bool(sid) and not conn.execute(
            "SELECT 1 FROM graph_nodes WHERE node_id=?", (sid,)
        ).fetchone()
        phantom = _phantom_label_from_proposal(title, summary)
        if missing or phantom:
            council_proposal_set_status(conn, pid, "expired")
            expired_proposals.append(pid)

    if commit and (removed_nodes or resolved_threads or expired_proposals):
        conn.commit()

    return {
        "removed_nodes": removed_nodes,
        "resolved_threads": resolved_threads,
        "expired_proposals": expired_proposals,
    }
