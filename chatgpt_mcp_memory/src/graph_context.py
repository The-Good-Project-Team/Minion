"""Graph-first context bundles for chat, menu bar, and external LLMs."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from store import ambient_events_since, graph_candidate_list, graph_scaffold_list, system_issues_open


def build_graph_context(
    conn,
    data_dir: Path,
    *,
    subject: str = "",
    max_candidates: int = 5,
) -> Dict[str, Any]:
    """Compact graph context: durable user model first, raw capture only as evidence hints."""
    scaffold = graph_scaffold_list(conn)
    candidates = graph_candidate_list(conn, status="open", limit=max_candidates)
    focus = _current_focus(data_dir)
    recent = _recent_ambient(conn)
    next_gap = None
    try:
        from graph_fill import pick_next_gap

        next_gap = pick_next_gap(conn, data_dir)
    except Exception:
        next_gap = None
    related_memory = []
    if subject:
        try:
            from corpus_context import prefetch_for_subject

            pack = prefetch_for_subject(conn, subject_label=subject, top_k=4)
            related_memory = [
                {
                    "chunk_id": h.get("chunk_id"),
                    "score": h.get("score"),
                    "path": h.get("path"),
                    "kind": h.get("kind"),
                    "text": (h.get("text") or "")[:240],
                }
                for h in pack.get("hits") or []
            ]
        except Exception:
            related_memory = []
    return {
        "graph": {
            "user_node_count": scaffold.get("user_node_count", 0),
            "totals": scaffold.get("totals") or {},
            "highlights": (scaffold.get("highlights") or [])[:12],
            "has_fill_gap": next_gap is not None,
            "next_gap": next_gap,
        },
        "open_candidates": candidates,
        "focus": focus,
        "recent_ambient": recent,
        "recent_ambient_hints": recent,
        "related_memory": related_memory,
        "generated_at": time.time(),
    }


def build_menu_status(conn, data_dir: Path) -> Dict[str, Any]:
    candidates = graph_candidate_list(conn, status="open", limit=20)
    issues = system_issues_open(conn, limit=8)
    ctx = build_graph_context(conn, data_dir, max_candidates=3)
    next_question = _next_question_payload(candidates, ctx["graph"].get("next_gap"))
    return {
        "pending_questions": len(candidates) + (1 if ctx["graph"].get("has_fill_gap") else 0),
        "open_candidates": len(candidates),
        "next_question": next_question,
        "should_notify": next_question is not None,
        "capture_health": "attention" if issues else "ok",
        "issues": issues,
        "graph": ctx["graph"],
        "focus": ctx.get("focus"),
        "generated_at": time.time(),
    }


def _next_question_payload(
    candidates: list[Dict[str, Any]],
    gap: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if candidates:
        c = candidates[0]
        return {
            "kind": "graph_candidate",
            "candidate_id": c.get("candidate_id"),
            "candidate_type": c.get("candidate_type"),
            "title": c.get("title") or "Resolve graph question",
            "body": c.get("body_md") or c.get("title") or "",
            "action": "resolve_graph_candidate",
        }
    if gap:
        label = gap.get("label") or gap.get("bucket_label") or gap.get("kind") or "graph gap"
        return {
            "kind": "graph_gap",
            "title": f"Fill graph: {label}",
            "body": f"Answer one line about {label} so Minion can fill the graph.",
            "action": "open_42",
            "gap": gap,
        }
    return None


def _current_focus(data_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        from screen_context_store import current_record

        rec = current_record(data_dir)
    except Exception:
        rec = None
    if not rec:
        return None
    return {
        "app_name": rec.get("app_name"),
        "window_title": rec.get("window_title"),
        "ts": rec.get("ts"),
    }


def _recent_ambient(conn) -> list[Dict[str, Any]]:
    try:
        rows = ambient_events_since(conn, since_ts=time.time() - 6 * 3600.0, limit=40)
    except Exception:
        return []
    out = []
    for e in rows[:8]:
        payload = e.get("payload") or {}
        out.append(
            {
                "event_id": e.get("event_id"),
                "event_type": e.get("event_type"),
                "captured_at": e.get("captured_at"),
                "app_name": payload.get("app_name") or payload.get("app"),
                "window_title": payload.get("window_title"),
            }
        )
    return out
