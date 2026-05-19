"""Graph clarification chat: corpus-backed questions, user replies."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from chat_store import (
    chat_message_insert,
    chat_open_count,
    chat_thread_get,
    chat_thread_insert,
    chat_thread_resolve,
    chat_threads_list,
)
from corpus_context import corpus_summary_line, prefetch_for_subject
from store import identity_claim_list, identity_claim_set_status


def list_threads(conn, *, status: str = "open", limit: int = 30) -> Dict[str, Any]:
    threads = chat_threads_list(conn, status=status, limit=limit)
    return {"threads": threads, "open_count": chat_open_count(conn)}


def get_thread(conn, thread_id: str) -> Optional[Dict[str, Any]]:
    return chat_thread_get(conn, thread_id)


def badge(conn) -> Dict[str, int]:
    return {"open_count": chat_open_count(conn)}


def next_clarification(conn, data_dir=None) -> Dict[str, Any]:
    """Create a new thread with one assistant question, or return existing open queue hint."""
    open_threads = chat_threads_list(conn, status="open", limit=1)
    if open_threads:
        full = chat_thread_get(conn, open_threads[0]["thread_id"])
        return {"thread": full, "created": False}

    claims = identity_claim_list(conn, status="proposed", limit=5)
    for c in claims:
        label = (c.get("text") or c.get("kind") or "claim")[:120]
        corpus = prefetch_for_subject(conn, subject_label=label, top_k=3)
        body = (
            f"I need your help clarifying something about **{c.get('kind', 'identity')}**.\n\n"
            f"> {label}\n\n"
        )
        cite = corpus_summary_line(corpus)
        if cite:
            body += f"\n{cite}\n\n"
        body += "Approve this claim, reject it, or reply with a correction below."

        tid = chat_thread_insert(
            conn,
            topic=f"Mirror · {c.get('kind', '')}",
            meta={"claim_id": c.get("claim_id"), "corpus_hits": corpus.get("hits", [])},
        )
        chat_message_insert(
            conn,
            thread_id=tid,
            role="assistant",
            body_md=body,
            meta={"claim_id": c.get("claim_id"), "corpus_hits": corpus.get("hits", [])},
        )
        return {"thread": chat_thread_get(conn, tid), "created": True}

    rows = conn.execute(
        "SELECT node_id, title, summary FROM graph_nodes "
        "WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub') "
        "AND (summary IS NULL OR summary='' OR summary='{}') LIMIT 5"
    ).fetchall()
    for row in rows:
        label = str(row["title"] or "Someone")
        sid = str(row["node_id"])
        corpus = prefetch_for_subject(conn, subject_label=label, subject_id=sid, top_k=3)
        body = f"Who is **{label}** in your life graph? A few words help me place them correctly."
        cite = corpus_summary_line(corpus)
        if cite:
            body += f"\n\n{cite}"
        tid = chat_thread_insert(
            conn,
            subject_id=sid,
            topic=f"Person · {label}",
            meta={"subject_id": sid},
        )
        chat_message_insert(conn, thread_id=tid, role="assistant", body_md=body, meta=corpus)
        return {"thread": chat_thread_get(conn, tid), "created": True}

    return {"thread": None, "created": False, "message": "No clarifications queued right now."}


def reply(
    conn,
    thread_id: str,
    *,
    body: str,
    action: Optional[str] = None,
) -> Dict[str, Any]:
    thread = chat_thread_get(conn, thread_id)
    if not thread:
        return {"ok": False, "error": "thread_not_found"}

    text = (body or "").strip()
    if text:
        chat_message_insert(conn, thread_id=thread_id, role="user", body_md=text)

    meta = thread.get("meta") or {}
    claim_id = meta.get("claim_id")
    if claim_id and action in ("approve", "reject", None):
        act = action or "approve"
        if act == "approve" or (text and not text.lower().startswith("no")):
            identity_claim_set_status(conn, claim_id, "active")
        elif act == "reject" or text.lower().startswith(("no", "reject")):
            identity_claim_set_status(conn, claim_id, "rejected")

    subject_id = thread.get("subject_id") or meta.get("subject_id")
    if subject_id and text:
        row = conn.execute(
            "SELECT summary FROM graph_nodes WHERE node_id=?", (subject_id,)
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
            "UPDATE graph_nodes SET summary=?, updated_at=? WHERE node_id=?",
            (json.dumps(existing, ensure_ascii=False), time.time(), subject_id),
        )

    chat_thread_resolve(conn, thread_id)
    return {"ok": True, "thread_id": thread_id, "thread": chat_thread_get(conn, thread_id)}
