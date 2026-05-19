"""42 — asks the questions; you answer; 42 gives guidance (Activity stream)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from chat_store import (
    chat_message_insert,
    chat_open_count,
    chat_thread_get,
    chat_thread_insert,
    chat_thread_resolve,
    chat_threads_list,
)
from corpus_context import prefetch_for_subject

log = logging.getLogger(__name__)

MODE = "forty_two"
_DEFAULT_MODEL = os.environ.get("MINION_CHAT_MODEL", "mistral:7b")


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
    """Surface the next 42 question (graph clarify queue or context-based prompt)."""
    active = active_thread(conn)
    if active:
        return {"thread": active, "created": False}

    from graph_clarify import next_clarification

    out = next_clarification(conn, data_dir)
    if out.get("thread"):
        tid = out["thread"]["thread_id"]
        row = chat_thread_get(conn, tid)
        if row:
            meta = dict(row.get("meta") or {})
            meta["mode"] = MODE
            conn.execute(
                "UPDATE chat_threads SET meta_json=?, topic=? WHERE thread_id=?",
                (
                    json.dumps(meta, ensure_ascii=False),
                    row.get("topic") or "42",
                    tid,
                ),
            )
        return {"thread": chat_thread_get(conn, tid), "created": bool(out.get("created"))}

    # Nothing in graph queue — ask from recent capture context
    prompt = _context_prompt(data_dir)
    tid = chat_thread_insert(conn, topic="42", meta={"mode": MODE, "source": "context"})
    chat_message_insert(conn, thread_id=tid, role="assistant", body_md=prompt)
    return {"thread": chat_thread_get(conn, tid), "created": True}


def _context_prompt(data_dir: Optional[Path]) -> str:
    if not data_dir:
        return (
            "**42:** What were you working on just now? "
            "A sentence is enough — I'll connect it to what I've captured."
        )
    try:
        from screen_context_store import current_record

        rec = current_record(Path(data_dir))
        if rec:
            app = str(rec.get("app_name") or "your Mac")
            title = str(rec.get("window_title") or "")
            return (
                f"**42:** I see **{app}**"
                + (f" — “{title[:80]}”" if title else "")
                + ". What were you trying to get done? I'll map it to your vault."
            )
    except Exception:
        pass
    return (
        "**42:** Pick one thing on your mind from the last hour — "
        "a person, project, or decision. I'll guide you from what I know."
    )


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

    meta = thread.get("meta") or {}
    mode = meta.get("mode")

    if meta.get("claim_id") or meta.get("subject_id"):
        from graph_clarify import reply as clarify_reply

        return clarify_reply(conn, thread_id, body=body, action=action)

    if mode not in (MODE, "ask") and mode is not None:
        from graph_clarify import reply as clarify_reply

        return clarify_reply(conn, thread_id, body=body, action=action)

    text = (body or "").strip()
    if not text and action != "dismiss":
        return {"ok": False, "error": "empty_message"}

    if action == "dismiss":
        chat_thread_resolve(conn, thread_id)
        return {"ok": True, "thread": chat_thread_get(conn, thread_id)}

    chat_message_insert(conn, thread_id=thread_id, role="user", body_md=text)
    corpus = prefetch_for_subject(conn, subject_label=text, top_k=8)
    hits = list(corpus.get("hits") or [])
    prior_q = _last_assistant_question(thread)
    guidance, used_llm = _compose_guidance(prior_q, text, hits)

    chat_message_insert(
        conn,
        thread_id=thread_id,
        role="assistant",
        body_md=guidance,
        meta={"corpus_hits": hits, "llm": used_llm, "speaker": "42"},
    )
    return {"ok": True, "thread": chat_thread_get(conn, thread_id), "llm": used_llm}


def dismiss(conn, thread_id: str) -> Dict[str, Any]:
    chat_thread_resolve(conn, thread_id)
    return {"ok": True, "thread_id": thread_id}


def _last_assistant_question(thread: Dict[str, Any]) -> str:
    for m in reversed(thread.get("messages") or []):
        if m.get("role") == "assistant":
            return str(m.get("body_md") or "")
    return ""


def _compose_guidance(
    question: str, answer: str, hits: List[Dict[str, Any]]
) -> Tuple[str, bool]:
    context_lines = []
    for i, h in enumerate(hits[:5], 1):
        path = str(h.get("path") or "note")
        snippet = str(h.get("text") or "").strip().replace("\n", " ")[:280]
        context_lines.append(f"{i}. [{path}] {snippet}")
    context = "\n".join(context_lines) or "(no close matches in vault yet)"

    if os.environ.get("MINION_ACTIVITY_CHAT_OFF", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return _fallback_guidance(answer, hits, context), False

    model = os.environ.get("MINION_ACTIVITY_CHAT_MODEL") or _DEFAULT_MODEL
    try:
        from llm import chat as llm_chat

        system = (
            "You are 42, Minion's guide. The user is bad at asking questions — "
            "you already asked something; they answered. Give short, practical guidance: "
            "what their answer implies, one concrete next step, and how vault evidence "
            "supports or contradicts them. Use ONLY the excerpts. No platitudes."
        )
        user = (
            f"Your question:\n{question[:800]}\n\n"
            f"Their answer:\n{answer}\n\n"
            f"Vault excerpts:\n{context}"
        )
        resp = llm_chat(
            model=model,
            system=system,
            user=user,
            options={"temperature": 0.35, "num_predict": 500},
            timeout_seconds=90.0,
        )
        return f"**42:** {resp.content.strip()}", True
    except Exception as exc:
        log.debug("forty_two guidance llm skipped: %s", exc)
        return _fallback_guidance(answer, hits, context), False


def _fallback_guidance(
    answer: str, hits: List[Dict[str, Any]], context: str
) -> str:
    if hits:
        lead = str(hits[0].get("text") or "")[:300]
        return (
            f"**42:** You said: “{answer[:200]}”. "
            f"Your vault points here: {lead}…\n\n"
            f"Next step: spend five minutes on the highest-signal note above. "
            f"(Local LLM off — set Ollama for richer guidance.)"
        )
    return (
        f"**42:** Noted — “{answer[:200]}”. "
        "I don't have much indexed on that yet; keep Minion open while you work "
        "or drop a file in Sources."
    )


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
                    graph_kinds=[],
                )
            )

    items.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
    return items[:limit]


def stream_state(conn, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    active = active_thread(conn)
    return {
        "open_count": chat_open_count(conn),
        "active_thread_id": active["thread_id"] if active else None,
        "needs_question": active is None and chat_open_count(conn) == 0,
    }
