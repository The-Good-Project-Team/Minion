"""Minion context bundle for 42 (graph, capture, corpus)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def build_minion_context(
    conn,
    *,
    data_dir: Optional[Path] = None,
    subject_label: str = "",
) -> Dict[str, Any]:
    """Compact JSON-safe snapshot for Gemini (no secrets)."""
    from graph_fill import pick_next_gap
    from store import graph_scaffold_list

    gap = pick_next_gap(conn, data_dir)
    scaffold = graph_scaffold_list(conn)
    highlights = (scaffold.get("highlights") or [])[:8]
    totals = scaffold.get("totals") or {}

    capture: Dict[str, Any] = {}
    if data_dir:
        try:
            from screen_context_store import current_record

            rec = current_record(Path(data_dir))
            if rec:
                capture = {
                    "app": rec.get("app_name"),
                    "title": (rec.get("window_title") or "")[:160],
                    "ts": rec.get("ts"),
                }
        except Exception:
            pass

    corpus_line = ""
    if subject_label:
        try:
            from corpus_context import corpus_summary_line, prefetch_for_subject

            corpus_line = corpus_summary_line(
                prefetch_for_subject(conn, subject_label=subject_label, top_k=3)
            )
        except Exception:
            pass

    recent_events: List[str] = []
    if data_dir:
        try:
            from graph_events import graph_event_feed_items

            for item in graph_event_feed_items(Path(data_dir), since_ts=time.time() - 86400, limit=5):
                recent_events.append(str(item.get("body") or "")[:200])
        except Exception:
            pass

    return {
        "graph": {
            "user_node_count": scaffold.get("user_node_count"),
            "totals": totals,
            "recent_nodes": [
                {
                    "kind": h.get("node_kind"),
                    "title": h.get("title"),
                    "bucket": h.get("bucket"),
                }
                for h in highlights
            ],
        },
        "next_gap": gap,
        "capture": capture,
        "corpus_hint": corpus_line[:500] if corpus_line else "",
        "recent_graph_events": recent_events,
    }


def context_markdown(ctx: Dict[str, Any]) -> str:
    return "```json\n" + json.dumps(ctx, ensure_ascii=False, indent=2) + "\n```"
