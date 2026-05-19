"""Automatic remember / compress / forget — no manual triage UI required."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from store import (
    meta_get,
    meta_set,
    promote_chunks_for_stale_sources,
)

log = logging.getLogger(__name__)

_META_LAST_RUN = "memory_lifecycle_last_run"
_RUN_INTERVAL_SEC = 6 * 3600.0

# Stale source age thresholds (days) for tier hops
HOT_TO_WARM_DAYS = 90
WARM_TO_COLD_DAYS = 365

# Vault-local ambient retention
AMBIENT_EVENTS_MAX_AGE_DAYS = 21
AMBIENT_AX_SOURCE_MAX_AGE_DAYS = 14
STREAM_ROTATE_BYTES = 12 * 1024 * 1024


def run_lightweight_cleanup(conn, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Cheap passes safe every ambient tick (~45s)."""
    now = now or time.time()
    return {"ambient_events_purged": _purge_old_ambient_events(conn, now=now)}


def run_auto_maintenance(conn, data_dir: Path, *, force: bool = False) -> Dict[str, Any]:
    """Idempotent background pass; runs at most every ``_RUN_INTERVAL_SEC`` unless ``force``."""
    now = time.time()
    last = 0.0
    raw = meta_get(conn, _META_LAST_RUN)
    if raw:
        try:
            last = float(raw)
        except ValueError:
            pass
    if not force and now - last < _RUN_INTERVAL_SEC:
        return {"skipped": True, "reason": "interval", "next_in_sec": int(_RUN_INTERVAL_SEC - (now - last))}

    out: Dict[str, Any] = {"skipped": False, "started_at": now}
    try:
        out["tier_hot_warm"] = _auto_tier_promote(
            conn, min_days=HOT_TO_WARM_DAYS, from_tier="hot", to_tier="warm"
        )
        out["tier_warm_cold"] = _auto_tier_promote(
            conn, min_days=WARM_TO_COLD_DAYS, from_tier="warm", to_tier="cold"
        )
        out["ambient_events_purged"] = _purge_old_ambient_events(conn, now=now)
        out["ambient_ax_pruned"] = _prune_stale_ambient_ax_sources(conn, data_dir, now=now)
        out["stream_rotated"] = _rotate_ambient_stream(data_dir)
        try:
            from ambient_consolidation import run_ambient_consolidation

            out["ambient_consolidation"] = run_ambient_consolidation(conn, data_dir)
        except Exception:
            log.exception("ambient_consolidation failed")
            out["ambient_consolidation"] = {"error": True}
    except Exception:
        log.exception("memory_lifecycle failed")
        out["error"] = True
    meta_set(conn, _META_LAST_RUN, str(now))
    out["finished_at"] = time.time()
    return out


def _auto_tier_promote(
    conn,
    *,
    min_days: int,
    from_tier: str,
    to_tier: str,
) -> Dict[str, Any]:
    thr = time.time() - float(min_days) * 86400.0
    kinds = ("ambient-ax", "ambient-audio", "chatgpt-export", "pdf", "md", "text")
    n = promote_chunks_for_stale_sources(
        conn,
        source_updated_before=thr,
        source_kinds=kinds,
        from_tier=from_tier,
        to_tier=to_tier,
    )
    return {"promoted": n, "min_days": min_days, "from": from_tier, "to": to_tier}


def _purge_old_ambient_events(conn, *, now: float) -> int:
    cutoff = now - AMBIENT_EVENTS_MAX_AGE_DAYS * 86400.0
    cur = conn.execute("SELECT COUNT(*) AS n FROM ambient_events WHERE captured_at < ?", (cutoff,)).fetchone()
    before = int(cur["n"]) if cur else 0
    if before == 0:
        return 0
    conn.execute("DELETE FROM ambient_events WHERE captured_at < ?", (cutoff,))
    return before


def _prune_stale_ambient_ax_sources(conn, data_dir: Path, *, now: float) -> int:
    """Drop indexed ambient-ax sources older than retention (compress/forget)."""
    cutoff = now - AMBIENT_AX_SOURCE_MAX_AGE_DAYS * 86400.0
    rows = conn.execute(
        "SELECT source_id, path, mtime FROM sources WHERE kind IN ('ambient-ax', 'ambient-audio') AND mtime < ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0
    from store import delete_source

    n = 0
    for r in rows:
        try:
            delete_source(conn, r["source_id"])
            n += 1
        except Exception:
            log.debug("failed to delete ambient source %s", r["path"])
    return n


def _rotate_ambient_stream(data_dir: Path) -> bool:
    """Rotate oversized ambient JSONL so ingest stays bounded."""
    path = Path(data_dir) / "ambient" / "stream.jsonl"
    if not path.is_file():
        return False
    try:
        if path.stat().st_size < STREAM_ROTATE_BYTES:
            return False
        backup = path.with_suffix(".jsonl.1")
        if backup.is_file():
            backup.unlink()
        path.rename(backup)
        path.touch()
        return True
    except OSError as e:
        log.warning("stream rotate failed %s: %s", path, e)
    return False
