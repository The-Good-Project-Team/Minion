"""Graph clarification chat: corpus-backed questions, user replies."""
from __future__ import annotations

from typing import Any, Dict, Optional

from chat_store import (
    chat_message_insert,
    chat_open_count,
    chat_thread_get,
    chat_threads_list,
)


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

    from graph_fill import open_thread_for_gap, pick_next_gap

    gap = pick_next_gap(conn, data_dir)
    if not gap:
        return {"thread": None, "created": False, "message": "No clarifications queued right now."}

    return open_thread_for_gap(conn, gap, data_dir=data_dir)


def reply(
    conn,
    thread_id: str,
    *,
    body: str,
    action: Optional[str] = None,
    data_dir=None,
) -> Dict[str, Any]:
    thread = chat_thread_get(conn, thread_id)
    if not thread:
        return {"ok": False, "error": "thread_not_found"}

    text = (body or "").strip()
    if text:
        chat_message_insert(conn, thread_id=thread_id, role="user", body_md=text)

    from graph_fill import apply_answer

    result = apply_answer(conn, thread, body=body or "", action=action, data_dir=data_dir)
    return {
        "ok": bool(result.get("ok")),
        "thread_id": thread_id,
        "thread": chat_thread_get(conn, thread_id),
        **result,
    }
