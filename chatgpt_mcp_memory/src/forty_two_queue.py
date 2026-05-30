"""Queue graph inference after corpus/ingest changes."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from store import meta_get, meta_set

log = logging.getLogger(__name__)

_META_PENDING = "graph_infer_pending"
_META_LAST_DRAIN = "graph_infer_last_drain"


def enqueue_graph_infer(conn, *, reason: str = "data_change") -> None:
    """Mark that embedded corpus may have new evidence for graph fill."""
    payload = {"ts": time.time(), "reason": reason}
    meta_set(conn, _META_PENDING, json.dumps(payload, ensure_ascii=False))


def has_graph_infer_pending(conn) -> bool:
    return bool(meta_get(conn, _META_PENDING))


def clear_graph_infer_pending(conn) -> None:
    meta_set(conn, _META_PENDING, "")


def drain_graph_infer_queue(
    conn,
    data_dir: Optional[Path],
    *,
    max_gaps: int = 5,
) -> Dict[str, Any]:
    """
    LLM graph mine tick: corpus evidence → validated graph writes.
    Runs continuously when Gemini is configured (core product loop).
    """
    from graph_corpus_mine import graph_mine_enabled, mine_limits, run_periodic_graph_mine_tick

    if not graph_mine_enabled(data_dir):
        clear_graph_infer_pending(conn)
        from graph_fill import pick_next_gap
        from forty_two_infer import try_fill_gap_from_corpus

        filled = 0
        deltas: list[str] = []
        last_status = "no_gemini"
        for _ in range(max_gaps):
            gap = pick_next_gap(conn, data_dir)
            if not gap:
                last_status = "no_gaps"
                break
            result = try_fill_gap_from_corpus(conn, gap, data_dir=data_dir)
            st = result.get("status")
            last_status = st or "unknown"
            if st == "filled":
                filled += 1
                deltas.extend(result.get("deltas") or [])
                continue
            break
        meta_set(conn, _META_LAST_DRAIN, str(time.time()))
        return {"filled": filled, "deltas": deltas, "status": last_status}

    limits = mine_limits(data_dir)
    max_calls = max(max_gaps, limits.get("max_42_tick", 4))
    out = run_periodic_graph_mine_tick(
        conn,
        data_dir,
        max_llm_calls=max_calls,
        source="42_scheduler",
    )
    if out.get("status") != "deferred":
        clear_graph_infer_pending(conn)
    meta_set(conn, _META_LAST_DRAIN, str(time.time()))
    return {
        "filled": out.get("filled", 0),
        "deltas": out.get("deltas") or [],
        "status": out.get("status"),
        "calls": out.get("calls"),
        "remaining_day": out.get("remaining_day"),
    }


def maybe_enqueue_after_ingest(conn, *, skipped: bool) -> None:
    if not skipped:
        enqueue_graph_infer(conn, reason="ingest")
