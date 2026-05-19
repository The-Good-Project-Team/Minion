"""Free-form questions on Activity — corpus search + optional local LLM."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from chat_store import (
    chat_message_insert,
    chat_thread_get,
    chat_thread_insert,
    chat_threads_list,
)
from corpus_context import prefetch_for_subject

log = logging.getLogger(__name__)

_DEFAULT_MODEL = os.environ.get("MINION_CHAT_MODEL", "mistral:7b")


def list_ask_threads(conn, *, limit: int = 20) -> Dict[str, Any]:
    threads = [
        t
        for t in chat_threads_list(conn, status="open", limit=limit * 2)
        if (t.get("meta") or {}).get("mode") == "ask"
    ][:limit]
    resolved = [
        t
        for t in chat_threads_list(conn, status="resolved", limit=limit * 2)
        if (t.get("meta") or {}).get("mode") == "ask"
    ][: max(0, limit - len(threads))]
    return {"threads": threads + resolved}


def ask(conn, *, message: str, thread_id: Optional[str] = None) -> Dict[str, Any]:
    text = (message or "").strip()
    if not text:
        return {"ok": False, "error": "empty_message"}

    if thread_id:
        thread = chat_thread_get(conn, thread_id)
        if not thread:
            return {"ok": False, "error": "thread_not_found"}
        if (thread.get("meta") or {}).get("mode") != "ask":
            return {"ok": False, "error": "not_ask_thread"}
    else:
        thread_id = chat_thread_insert(
            conn,
            topic="Ask",
            meta={"mode": "ask"},
        )

    chat_message_insert(conn, thread_id=thread_id, role="user", body_md=text)
    corpus = prefetch_for_subject(conn, subject_label=text, top_k=8)
    hits: List[Dict[str, Any]] = list(corpus.get("hits") or [])
    body, used_llm = _compose_answer(text, hits)

    chat_message_insert(
        conn,
        thread_id=thread_id,
        role="assistant",
        body_md=body,
        meta={"corpus_hits": hits, "llm": used_llm},
    )
    return {"ok": True, "thread": chat_thread_get(conn, thread_id), "llm": used_llm}


def _compose_answer(query: str, hits: List[Dict[str, Any]]) -> Tuple[str, bool]:
    if not hits:
        return (
            "I don't have indexed notes that match that yet. "
            "Keep the app running so capture and ingest can fill your vault, or drop relevant files in Sources.",
            False,
        )

    context_lines = []
    for i, h in enumerate(hits[:6], 1):
        path = str(h.get("path") or "note")
        snippet = str(h.get("text") or "").strip().replace("\n", " ")
        if len(snippet) > 320:
            snippet = snippet[:317] + "…"
        context_lines.append(f"{i}. [{path}] {snippet}")
    context = "\n".join(context_lines)

    model = os.environ.get("MINION_ACTIVITY_CHAT_MODEL") or _DEFAULT_MODEL
    if os.environ.get("MINION_ACTIVITY_CHAT_OFF", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return _fallback_answer(query, hits, context), False

    try:
        from llm import chat as llm_chat

        system = (
            "You are Minion, a private local memory assistant. Answer using ONLY the "
            "excerpts below. If evidence is thin, say what is missing. Be concise. "
            "Cite paths in parentheses when you use a source."
        )
        user = f"Question: {query}\n\nVault excerpts:\n{context}"
        resp = llm_chat(
            model=model,
            system=system,
            user=user,
            options={"temperature": 0.2, "num_predict": 600},
            timeout_seconds=90.0,
        )
        return resp.content.strip(), True
    except Exception as exc:
        log.debug("activity_chat llm skipped: %s", exc)
        return _fallback_answer(query, hits, context), False


def _fallback_answer(
    query: str, hits: List[Dict[str, Any]], context: str
) -> str:
    top = hits[0]
    lead = str(top.get("text") or "")[:400].strip()
    paths = ", ".join(str(h.get("path") or "?") for h in hits[:3])
    return (
        f"**Relevant to “{query}”** (semantic search; local LLM unavailable)\n\n"
        f"{lead}\n\n"
        f"Also see: {paths}\n\n"
        f"---\n\n{context}"
    )
