"""42 — asks the questions; you answer; the life graph gets filled in."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

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

    out = open_thread_for_gap(conn, gap, data_dir=data_dir)
    if out.get("auto_filled") and not out.get("thread"):
        return {
            "thread": None,
            "created": False,
            "auto_filled": True,
            "message": out.get("message") or "Filled graph from your saved context.",
            "deltas": out.get("deltas"),
        }
    return out


def _complete_reply(
    conn,
    thread_id: str,
    *,
    body: str,
    action: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], str, str]:
    """Insert user + assistant messages; return (result, user_message_id, guidance)."""
    thread = chat_thread_get(conn, thread_id)
    if not thread:
        return ({"ok": False, "error": "thread_not_found"}, "", "")

    if action == "dismiss":
        chat_thread_resolve(conn, thread_id)
        return ({"ok": True, "resolved": True, "dismissed": True}, "", "")

    text = (body or "").strip()
    if not text:
        return ({"ok": False, "error": "empty_message"}, "", "")

    user_mid = chat_message_insert(conn, thread_id=thread_id, role="user", body_md=text)
    thread = chat_thread_get(conn, thread_id) or thread

    from graph_fill import apply_answer
    from forty_two_llm import compose_42_reply, normalize_user_turn

    body_for_graph, action_override = normalize_user_turn(
        conn, thread, text, data_dir=data_dir
    )
    if action_override and not action:
        action = action_override
    result = apply_answer(
        conn, thread, body=body_for_graph, action=action, data_dir=data_dir
    )
    if not result.get("ok"):
        return (result, user_mid, "")

    thread = chat_thread_get(conn, thread_id) or thread
    guidance, used_llm = compose_42_reply(
        conn, thread, result, user_text=text, data_dir=data_dir
    )
    chat_message_insert(
        conn,
        thread_id=thread_id,
        role="assistant",
        body_md=guidance,
        meta={
            "speaker": "42",
            "graph_fill": True,
            "resolved": result.get("resolved"),
            "llm": used_llm,
            "provider": "gemini" if used_llm else "template",
        },
    )
    return (result, user_mid, guidance)


def reply(
    conn,
    thread_id: str,
    *,
    body: str,
    action: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    result, _user_mid, _guidance = _complete_reply(
        conn, thread_id, body=body, action=action, data_dir=data_dir
    )
    if not result.get("ok"):
        return {**result, "thread": chat_thread_get(conn, thread_id)}
    if result.get("dismissed"):
        return {"ok": True, "thread": chat_thread_get(conn, thread_id)}
    return {
        "ok": True,
        "thread": chat_thread_get(conn, thread_id),
        "resolved": result.get("resolved"),
    }


def stream_reply(
    conn,
    thread_id: str,
    *,
    body: str,
    action: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield chat SSE event payloads for graph-fill replies."""
    from store import _new_id

    thread = chat_thread_get(conn, thread_id)
    if not thread:
        yield {"event": "error", "data": {"code": "thread_not_found", "message": "thread not found"}}
        return

    if action == "dismiss":
        chat_thread_resolve(conn, thread_id)
        yield {
            "event": "thread.resolved",
            "data": {"thread_id": thread_id, "thread": chat_thread_get(conn, thread_id)},
        }
        return

    text = (body or "").strip()
    if not text:
        yield {"event": "error", "data": {"code": "empty_message", "message": "empty message"}}
        return

    user_mid = chat_message_insert(conn, thread_id=thread_id, role="user", body_md=text)
    yield {
        "event": "message.user",
        "data": {"message_id": user_mid, "thread_id": thread_id, "body_md": text},
    }

    thread = chat_thread_get(conn, thread_id) or thread
    from graph_fill import apply_answer
    from forty_two_llm import compose_42_reply, iter_42_reply_deltas, normalize_user_turn

    body_for_graph, action_override = normalize_user_turn(
        conn, thread, text, data_dir=data_dir
    )
    if action_override and not action:
        action = action_override
    result = apply_answer(
        conn, thread, body=body_for_graph, action=action, data_dir=data_dir
    )
    if not result.get("ok"):
        yield {"event": "error", "data": {"code": result.get("error", "failed"), "message": str(result.get("error"))}}
        return

    thread = chat_thread_get(conn, thread_id) or thread
    asst_mid = _new_id("cmsg")
    yield {
        "event": "message.assistant.start",
        "data": {"message_id": asst_mid, "thread_id": thread_id},
    }

    parts: List[str] = []
    delta_iter, used_flag = iter_42_reply_deltas(
        conn, thread, result, user_text=text, data_dir=data_dir
    )
    for delta in delta_iter:
        parts.append(delta)
        yield {
            "event": "message.assistant.delta",
            "data": {"message_id": asst_mid, "thread_id": thread_id, "delta": delta},
        }

    guidance = "".join(parts).strip()
    used_llm = bool(used_flag[0])
    if not guidance:
        guidance, used_llm = compose_42_reply(
            conn, thread, result, user_text=text, data_dir=data_dir
        )

    chat_message_insert(
        conn,
        thread_id=thread_id,
        role="assistant",
        body_md=guidance,
        meta={
            "speaker": "42",
            "graph_fill": True,
            "resolved": result.get("resolved"),
            "llm": used_llm,
            "provider": "gemini" if used_llm else "template",
        },
    )
    full = chat_thread_get(conn, thread_id)
    msgs = full.get("messages") or [] if full else []
    asst_msg = msgs[-1] if msgs else {"message_id": asst_mid, "body_md": guidance, "role": "assistant"}
    yield {
        "event": "message.assistant.done",
        "data": {
            "message": asst_msg,
            "thread": full,
            "resolved": bool(result.get("resolved")),
        },
    }
    if result.get("resolved"):
        yield {"event": "thread.resolved", "data": {"thread_id": thread_id}}


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
                    actions=_conversation_actions(m, full) if is_42 else [],
                    refs={"thread_id": tid, "message_id": str(m.get("message_id") or "")},
                    graph_kinds=_graph_kinds_for_thread(full),
                )
            )

    items.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
    return items[:limit]


def _conversation_actions(message: Dict[str, Any], thread: Dict[str, Any]) -> List[Dict[str, str]]:
    if thread.get("status") != "open":
        return []
    actions: List[Dict[str, str]] = []
    for s in (message.get("meta") or {}).get("suggestions") or []:
        name = str(s.get("name") or "").strip()
        if name:
            actions.append({"id": f"pick:{name}", "label": name[:48]})
    actions.append({"id": "dismiss", "label": "Done"})
    return actions


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
    suggestions: List[Dict[str, str]] = []
    if active:
        for m in reversed(active.get("messages") or []):
            if m.get("role") == "assistant":
                suggestions = list((m.get("meta") or {}).get("suggestions") or [])
                break
    question_body = ""
    if active:
        for m in reversed(active.get("messages") or []):
            if m.get("role") == "assistant":
                question_body = str(m.get("body_md") or "").strip()
                break

    from gemini_client import forty_two_gemini_model, gemini_configured

    return {
        "open_count": chat_open_count(conn),
        "active_thread_id": active["thread_id"] if active else None,
        "needs_question": active is None and has_gap,
        "suggestions": suggestions,
        "question_body_md": question_body,
        "has_gap": has_gap,
        "llm_available": gemini_configured(data_dir),
        "gemini_model": forty_two_gemini_model(data_dir) if gemini_configured(data_dir) else None,
    }


def pinned_conversation_item(conn) -> Optional[Dict[str, Any]]:
    """Latest 42 question for the active open thread (always show, not time-filtered)."""
    active = active_thread(conn)
    if not active:
        return None
    msgs = active.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") != "assistant":
            continue
        body = str(m.get("body_md") or "").strip()
        if not body:
            continue
        from activity_feed import _feed_item

        tid = active["thread_id"]
        return _feed_item(
            feed_id=f"msg-pin-{m.get('message_id')}",
            ts=float(m.get("created_at") or time.time()),
            lane="conversation",
            kind="forty_two",
            title="42",
            body=body,
            parse=None,
            actions=_conversation_actions(m, active),
            refs={"thread_id": tid, "message_id": str(m.get("message_id") or ""), "pinned": True},
            graph_kinds=_graph_kinds_for_thread(active),
        )
    return None
