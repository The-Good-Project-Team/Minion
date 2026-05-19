"""Graph-fill queue: gaps 42 asks about so the life graph gets filled in."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from chat_store import chat_message_insert, chat_thread_get, chat_thread_insert, chat_thread_resolve
from corpus_context import corpus_summary_line, prefetch_for_subject
from entity_resolution import ensure_person_node, link_belongs_to_scaffold
from store import _new_id, identity_claim_list, identity_claim_set_status

log = logging.getLogger(__name__)

ME_NODE = "scaffold-me"

# Scaffold buckets 42 nudges when empty (parent_id, child node_kind, label for copy).
_BUCKET_GAPS: List[Tuple[str, str, str]] = [
    ("scaffold-people-friends", "person", "a friend"),
    ("scaffold-people-family", "person", "a family member"),
    ("scaffold-projects-active", "project", "something you're actively working on"),
    ("scaffold-places-home", "place", "where you live"),
    ("scaffold-work-companies", "organization", "where you work"),
    ("scaffold-groups-teams", "group", "a team or community you belong to"),
]


def pick_next_gap(conn, data_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Highest-priority unfilled graph slot, or None."""
    claims = identity_claim_list(conn, status="proposed", limit=1)
    if claims:
        c = claims[0]
        label = (c.get("text") or c.get("kind") or "claim")[:120]
        return {
            "gap_type": "claim",
            "claim_id": c.get("claim_id"),
            "label": label,
            "kind": c.get("kind"),
        }

    row = conn.execute(
        "SELECT node_id, title, summary FROM graph_nodes "
        "WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub') "
        "AND (summary IS NULL OR summary='' OR summary='{}') "
        "ORDER BY updated_at ASC LIMIT 1"
    ).fetchone()
    if row:
        return {
            "gap_type": "person",
            "subject_id": str(row["node_id"]),
            "label": str(row["title"] or "Someone"),
            "phase": 0,
        }

    row = conn.execute(
        "SELECT n.node_id, n.title FROM graph_nodes n "
        "WHERE n.node_kind='person' AND n.status NOT IN ('scaffold', 'stub') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM graph_edges e WHERE e.from_node_id=? AND e.to_node_id=n.node_id "
        "  AND e.rel_kind IN ('knows', 'related_to')"
        ") LIMIT 1",
        (ME_NODE,),
    ).fetchone()
    if row:
        return {
            "gap_type": "person_relation",
            "subject_id": str(row["node_id"]),
            "label": str(row["title"] or "Someone"),
            "phase": 0,
        }

    for parent_id, node_kind, hint in _BUCKET_GAPS:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM graph_nodes "
            "WHERE parent_node_id=? AND status NOT IN ('scaffold', 'stub')",
            (parent_id,),
        ).fetchone()
        if n and int(n["c"]) == 0:
            parent = conn.execute(
                "SELECT title FROM graph_nodes WHERE node_id=?", (parent_id,)
            ).fetchone()
            bucket = str(parent["title"] if parent else parent_id)
            return {
                "gap_type": "bucket",
                "parent_node_id": parent_id,
                "node_kind": node_kind,
                "bucket_label": bucket,
                "hint": hint,
            }

    if data_dir:
        cap = _capture_gap(data_dir)
        if cap:
            return cap

    return None


def _capture_gap(data_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        from screen_context_store import current_record

        rec = current_record(data_dir)
        if not rec:
            return None
        app = str(rec.get("app_name") or "").strip()
        title = str(rec.get("window_title") or "").strip()
        if not title and not app:
            return None
        return {
            "gap_type": "capture",
            "app_name": app,
            "window_title": title[:120],
        }
    except Exception:
        log.debug("capture gap skipped", exc_info=True)
        return None


def compose_question(conn, gap: Dict[str, Any], *, data_dir: Optional[Path] = None) -> str:
    """42-voiced question for a gap."""
    gtype = gap.get("gap_type")
    if gtype == "claim":
        label = gap.get("label") or "this"
        corpus = prefetch_for_subject(conn, subject_label=str(label), top_k=3)
        body = (
            f"**42:** Mirror flagged **{gap.get('kind', 'identity')}** — should this land on your graph?\n\n"
            f"> {label}\n\n"
        )
        cite = corpus_summary_line(corpus)
        if cite:
            body += f"{cite}\n\n"
        body += "Reply **yes** to add it, **no** to drop it, or correct me in a sentence."
        return body

    if gtype == "person":
        label = gap.get("label") or "someone"
        sid = gap.get("subject_id")
        corpus = prefetch_for_subject(
            conn, subject_label=str(label), subject_id=sid, top_k=3
        )
        body = f"**42:** Who is **{label}** to you? One line is enough — I'll file it on your graph."
        cite = corpus_summary_line(corpus)
        if cite:
            body += f"\n\n{cite}"
        return body

    if gtype == "person_relation":
        label = gap.get("label") or "someone"
        return (
            f"**42:** How do you know **{label}**? "
            "(e.g. coworker, cousin, neighbor — I'll link them to you on the graph.)"
        )

    if gtype == "bucket":
        bucket = gap.get("bucket_label") or "this section"
        hint = gap.get("hint") or "someone or something"
        return (
            f"**42:** Your **{bucket}** bucket is empty. "
            f"Name {hint} — I'll add a node and wire it into the graph."
        )

    if gtype == "capture":
        app = gap.get("app_name") or "your Mac"
        title = gap.get("window_title") or ""
        line = f"**{app}**" + (f" — “{title}”" if title else "")
        return (
            f"**42:** I noticed {line}. "
            "Who or what is this on your graph — a **person**, **project**, or skip?"
        )

    return "**42:** What's one person or project I should add to your graph right now?"


def open_thread_for_gap(conn, gap: Dict[str, Any], *, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Create thread + first assistant message for a gap."""
    meta: Dict[str, Any] = {"mode": "forty_two", "gap": gap}
    if gap.get("claim_id"):
        meta["claim_id"] = gap["claim_id"]
    topic = "42 · graph"
    subject_id = gap.get("subject_id")
    if gap.get("gap_type") == "claim":
        topic = f"42 · {gap.get('kind', 'mirror')}"
    elif gap.get("gap_type") == "person":
        topic = f"42 · {gap.get('label', 'person')}"
    elif gap.get("gap_type") == "bucket":
        topic = f"42 · {gap.get('bucket_label', 'graph')}"

    tid = chat_thread_insert(
        conn,
        subject_id=subject_id,
        topic=topic,
        meta=meta,
    )
    body = compose_question(conn, gap, data_dir=data_dir)
    chat_message_insert(
        conn,
        thread_id=tid,
        role="assistant",
        body_md=body,
        meta={"gap": gap, "speaker": "42"},
    )
    return {"thread": chat_thread_get(conn, tid), "created": True, "gap": gap}


def apply_answer(
    conn,
    thread: Dict[str, Any],
    *,
    body: str,
    action: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write user answer into the graph; return confirmation lines for 42."""
    text = (body or "").strip()
    meta = dict(thread.get("meta") or {})
    gap = dict(meta.get("gap") or {})
    gtype = gap.get("gap_type") or meta.get("gap_type")

    if meta.get("claim_id") and not gap:
        gap = {"gap_type": "claim", "claim_id": meta.get("claim_id"), "label": meta.get("label")}

    deltas: List[str] = []

    claim_id = meta.get("claim_id") or gap.get("claim_id")
    if claim_id and (gtype == "claim" or not gtype):
        reject = action == "reject" or (
            text and text.lower().startswith(("no", "reject"))
        )
        if reject:
            identity_claim_set_status(conn, claim_id, "rejected")
            deltas.append("Rejected that mirror claim — it won't be added to your graph.")
        else:
            identity_claim_set_status(conn, claim_id, "active")
            deltas.append("Approved — claim is on your graph.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True}
        persist_graph_snapshot(conn, data_dir)
        return out

    if not text and action != "dismiss":
        return {"ok": False, "error": "empty_message"}

    if action == "dismiss" or text.lower() in ("skip", "later", "not now"):
        chat_thread_resolve(conn, thread["thread_id"])
        return {
            "ok": True,
            "deltas": ["Skipped for now — I'll ask about another gap later."],
            "resolved": True,
        }

    subject_id = thread.get("subject_id") or meta.get("subject_id") or gap.get("subject_id")

    if gtype == "person" and subject_id:
        _person_summary(conn, subject_id, text)
        deltas.append(f"Saved who **{_node_title(conn, subject_id)}** is to you on the graph.")
        gap["gap_type"] = "person_relation"
        gap["subject_id"] = subject_id
        gap["label"] = _node_title(conn, subject_id)
        gap["phase"] = 1
        meta["gap"] = gap
        conn.execute(
            "UPDATE chat_threads SET meta_json=? WHERE thread_id=?",
            (json.dumps(meta, ensure_ascii=False), thread["thread_id"]),
        )
        out = {
            "ok": True,
            "deltas": deltas,
            "resolved": False,
            "follow_up": compose_question(conn, gap),
        }
        persist_graph_snapshot(conn, data_dir)
        return out

    if gtype == "person_relation" and subject_id:
        _link_knows(conn, ME_NODE, subject_id, text)
        deltas.append(f"Linked you → **{_node_title(conn, subject_id)}** (`knows`).")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True}
        persist_graph_snapshot(conn, data_dir)
        return out

    if gtype == "bucket":
        parent_id = str(gap.get("parent_node_id") or "scaffold-people-friends")
        node_kind = str(gap.get("node_kind") or "person")
        title = _first_phrase(text)
        if node_kind == "person" and parent_id == "scaffold-people-friends":
            nid = ensure_person_node(
                conn,
                label=title,
                meta={"user_note": text[:500], "source": "forty_two"},
            )
        else:
            nid = _create_graph_node(conn, parent_id, node_kind, title, user_note=text)
            if node_kind == "person":
                link_belongs_to_scaffold(conn, nid, _people_root(parent_id))
                _link_knows(conn, ME_NODE, nid, text)
        deltas.append(f"Added **{title}** under **{gap.get('bucket_label', 'graph')}**.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True, "node_id": nid}
        persist_graph_snapshot(conn, data_dir)
        return out

    if gtype == "capture":
        parsed = _parse_capture_answer(text)
        if parsed.get("skip"):
            deltas.append("Got it — left off the graph for now.")
            chat_thread_resolve(conn, thread["thread_id"])
            return {"ok": True, "deltas": deltas, "resolved": True}
        kind = parsed.get("node_kind") or "person"
        title = parsed.get("title") or _first_phrase(text)
        parent = _default_parent_for_kind(kind)
        nid = _create_graph_node(conn, parent, kind, title, user_note=text)
        if kind == "person":
            link_belongs_to_scaffold(conn, nid, "scaffold-people")
            _link_knows(conn, ME_NODE, nid, text)
        deltas.append(f"Added **{title}** ({kind}) from what you were looking at.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True, "node_id": nid}
        persist_graph_snapshot(conn, data_dir)
        return out

    if subject_id and not gtype:
        _person_summary(conn, subject_id, text)
        deltas.append(f"Saved who **{_node_title(conn, subject_id)}** is on your graph.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True}
        persist_graph_snapshot(conn, data_dir)
        return out

    if subject_id:
        _person_summary(conn, subject_id, text)
        deltas.append("Updated your graph.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True}
        persist_graph_snapshot(conn, data_dir)
        return out

    chat_thread_resolve(conn, thread["thread_id"])
    return {"ok": True, "deltas": deltas or ["Noted."], "resolved": True}


def persist_graph_snapshot(conn, data_dir: Optional[Path]) -> None:
    """Best-effort JSON backup of user graph rows (survives mistaken DB rotate)."""
    if not data_dir:
        return
    try:
        nodes = [
            dict(r)
            for r in conn.execute(
                "SELECT node_id, node_kind, title, status, summary, parent_node_id, "
                "aliases_json, confidence, updated_at FROM graph_nodes "
                "WHERE status NOT IN ('scaffold', 'stub')"
            ).fetchall()
        ]
        edges = [
            dict(r)
            for r in conn.execute(
                "SELECT edge_id, from_node_id, to_node_id, rel_kind, created_at, updated_at "
                "FROM graph_edges"
            ).fetchall()
        ]
        path = Path(data_dir) / "graph_snapshot.json"
        path.write_text(
            json.dumps(
                {"saved_at": time.time(), "nodes": nodes, "edges": edges},
                ensure_ascii=False,
                indent=0,
            ),
            encoding="utf-8",
        )
    except Exception:
        log.warning("graph_snapshot write failed", exc_info=True)


def format_confirmation(result: Dict[str, Any]) -> str:
    lines = [f"**42:** {d}" for d in result.get("deltas") or ["Updated your graph."]]
    follow = result.get("follow_up")
    if follow and not result.get("resolved"):
        lines.append("")
        lines.append(follow)
    elif result.get("resolved"):
        nxt = "Ask me again when you're ready — I'll find the next empty spot."
        lines.append("")
        lines.append(f"**42:** {nxt}")
    return "\n".join(lines)


def _person_summary(conn, node_id: str, text: str) -> None:
    row = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    existing: Dict[str, Any] = {}
    if row and row["summary"] and str(row["summary"]).strip().startswith("{"):
        try:
            existing = json.loads(row["summary"])
        except json.JSONDecodeError:
            pass
    existing["clarified_at"] = time.time()
    existing["user_note"] = text[:500]
    conn.execute(
        "UPDATE graph_nodes SET summary=?, updated_at=?, status='active' WHERE node_id=?",
        (json.dumps(existing, ensure_ascii=False), time.time(), node_id),
    )


def _link_knows(conn, from_id: str, to_id: str, note: str) -> None:
    if not from_id or not to_id or from_id == to_id:
        return
    exists = conn.execute(
        "SELECT 1 FROM graph_edges WHERE from_node_id=? AND to_node_id=? AND rel_kind='knows'",
        (from_id, to_id),
    ).fetchone()
    if exists:
        return
    eid = _new_id("ge")
    now = time.time()
    edge_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(graph_edges)").fetchall()}
    if "updated_at" in edge_cols:
        conn.execute(
            "INSERT INTO graph_edges(edge_id, from_node_id, to_node_id, rel_kind, created_at, updated_at) "
            "VALUES(?, ?, ?, 'knows', ?, ?)",
            (eid, from_id, to_id, now, now),
        )
    else:
        conn.execute(
            "INSERT INTO graph_edges(edge_id, from_node_id, to_node_id, rel_kind, created_at) "
            "VALUES(?, ?, ?, 'knows', ?)",
            (eid, from_id, to_id, now),
        )
    if note:
        row = conn.execute(
            "SELECT summary FROM graph_nodes WHERE node_id=?", (to_id,)
        ).fetchone()
        meta: Dict[str, Any] = {}
        if row and row["summary"] and str(row["summary"]).strip().startswith("{"):
            try:
                meta = json.loads(row["summary"])
            except json.JSONDecodeError:
                pass
        meta["relation_note"] = note[:200]
        conn.execute(
            "UPDATE graph_nodes SET summary=?, updated_at=? WHERE node_id=?",
            (json.dumps(meta, ensure_ascii=False), time.time(), to_id),
        )


def _create_graph_node(
    conn, parent_id: str, node_kind: str, title: str, *, user_note: str = ""
) -> str:
    if node_kind == "person":
        return ensure_person_node(
            conn,
            label=title,
            meta={"user_note": user_note[:500], "source": "forty_two"},
        )
    nid = _new_id("gn")
    meta = {"user_note": user_note[:500], "source": "forty_two"} if user_note else {}
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, ?, ?, 'active', '', NULL, ?, '[]', ?, 0.6, '[]', "
        "'vault_local', ?, ?)",
        (
            nid,
            node_kind,
            title[:200],
            parent_id,
            json.dumps(meta, ensure_ascii=False) if meta else "",
            now,
            now,
        ),
    )
    return nid


def _default_parent_for_kind(node_kind: str) -> str:
    return {
        "person": "scaffold-people-friends",
        "project": "scaffold-projects-active",
        "place": "scaffold-places-frequent",
        "organization": "scaffold-work-companies",
        "group": "scaffold-groups-teams",
    }.get(node_kind, "scaffold-me")


def _people_root(parent_id: str) -> str:
    if parent_id.startswith("scaffold-people"):
        return "scaffold-people"
    return parent_id


def _node_title(conn, node_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    return str(row["title"]) if row else "them"


def _first_phrase(text: str) -> str:
    t = text.strip()
    for sep in (".", ",", "—", "-", "–"):
        if sep in t:
            t = t.split(sep)[0]
    return (t.strip() or "Untitled")[:200]


def _parse_capture_answer(text: str) -> Dict[str, Any]:
    low = text.lower().strip()
    if low in ("skip", "no", "nothing", "n/a"):
        return {"skip": True}
    if "project" in low:
        title = text.replace("project", "").replace("Project", "").strip(" :—-") or _first_phrase(text)
        return {"node_kind": "project", "title": _first_phrase(title)}
    if "place" in low:
        return {"node_kind": "place", "title": _first_phrase(text)}
    if "org" in low or "company" in low or "work" in low:
        return {"node_kind": "organization", "title": _first_phrase(text)}
    return {"node_kind": "person", "title": _first_phrase(text)}
