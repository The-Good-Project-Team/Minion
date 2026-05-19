"""42 — asks the questions; you answer; the life graph gets filled in."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from chat_store import (
    chat_message_insert,
    chat_open_count,
    chat_thread_get,
    chat_thread_resolve,
    chat_threads_list,
)

log = logging.getLogger(__name__)

MODE = "forty_two"


def active_thread(conn) -> Optional[Dict[str, Any]]:
    """Most recent open thread 42 expects a reply on."""
    for t in chat_threads_list(conn, status="open", limit=20):
        full = chat_thread_get(conn, t["thread_id"])
        if not full:
            continue
        msgs = full.get("messages") or []
        if not msgs:
            return full
        if msgs[-1].get("role") == "assistant":
            return full
    return None


def next_question(conn, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Surface the next 42 question from an open thread or the graph-fill queue."""
    active = active_thread(conn)
    if active:
        return {"thread": active, "created": False}

    from graph_fill import open_thread_for_gap, pick_next_gap

    gap = pick_next_gap(conn, data_dir)
    if not gap:
        return {"thread": None, "created": False, "message": "No graph gaps queued right now."}

    return open_thread_for_gap(conn, gap, data_dir=data_dir)


def reply(
    conn,
    thread_id: str,
    *,
    body: str,
    action: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    thread = chat_thread_get(conn, thread_id)
    if not thread:
        return {"ok": False, "error": "thread_not_found"}

    if action == "dismiss":
        chat_thread_resolve(conn, thread_id)
        return {"ok": True, "thread": chat_thread_get(conn, thread_id)}

    text = (body or "").strip()
    if not text:
        return {"ok": False, "error": "empty_message"}

    chat_message_insert(conn, thread_id=thread_id, role="user", body_md=text)

    from graph_fill import apply_answer, format_confirmation

    result = apply_answer(conn, thread, body=text, action=action, data_dir=data_dir)
    if not result.get("ok"):
        return {**result, "thread": chat_thread_get(conn, thread_id)}

    guidance = format_confirmation(result)
    chat_message_insert(
        conn,
        thread_id=thread_id,
        role="assistant",
        body_md=guidance,
        meta={"speaker": "42", "graph_fill": True, "resolved": result.get("resolved")},
    )
    return {
        "ok": True,
        "thread": chat_thread_get(conn, thread_id),
        "resolved": result.get("resolved"),
    }


def dismiss(conn, thread_id: str) -> Dict[str, Any]:
    chat_thread_resolve(conn, thread_id)
    return {"ok": True, "thread_id": thread_id}


def conversation_feed_items(
    conn,
    *,
    since_ts: float,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """Flatten recent chat messages into river items (Slack-style stream)."""
    from activity_feed import _feed_item

    threads = chat_threads_list(conn, status="open", limit=15)
    threads += chat_threads_list(conn, status="resolved", limit=10)
    seen_threads: set[str] = set()
    items: List[Dict[str, Any]] = []

    for t in threads:
        tid = t["thread_id"]
        if tid in seen_threads:
            continue
        seen_threads.add(tid)
        full = chat_thread_get(conn, tid)
        if not full:
            continue
        for m in full.get("messages") or []:
            ts = float(m.get("created_at") or 0)
            if ts < since_ts:
                continue
            role = str(m.get("role") or "")
            body = str(m.get("body_md") or "").strip()
            if not body:
                continue
            is_42 = role == "assistant"
            items.append(
                _feed_item(
                    feed_id=f"msg-{m.get('message_id')}",
                    ts=ts,
                    lane="conversation",
                    kind="forty_two" if is_42 else "you",
                    title="42" if is_42 else "You",
                    body=body,
                    parse=None,
                    actions=(
                        [{"id": "dismiss", "label": "Done"}]
                        if is_42 and full.get("status") == "open"
                        else []
                    ),
                    refs={"thread_id": tid, "message_id": str(m.get("message_id") or "")},
                    graph_kinds=_graph_kinds_for_thread(full),
                )
            )

    items.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
    return items[:limit]


def _graph_kinds_for_thread(thread: Dict[str, Any]) -> List[str]:
    gap = (thread.get("meta") or {}).get("gap") or {}
    gtype = gap.get("gap_type")
    if gtype == "person" or gtype == "person_relation":
        return ["person"]
    if gtype == "bucket":
        return [str(gap.get("node_kind") or "person")]
    if gtype == "claim":
        return ["person", "family"]
    if gtype == "capture":
        return ["person", "project"]
    return ["person"]


def stream_state(conn, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    active = active_thread(conn)
    from graph_fill import pick_next_gap

    has_gap = pick_next_gap(conn, data_dir) is not None
    return {
        "open_count": chat_open_count(conn),
        "active_thread_id": active["thread_id"] if active else None,
        "needs_question": active is None and chat_open_count(conn) == 0 and has_gap,
    }
