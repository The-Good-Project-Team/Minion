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

# Noise / derived-data retention (Workstream 3). We TRIM derived signal, never the
# user's own content. PROTECTED_SOURCE_KINDS are tiered but never auto-deleted; the
# trim helpers below only touch derived tables (screen_memory_events, graph_candidates,
# superseded identity_claims) and derived source kinds (ambient-*). User docs, active
# graph, wiki, and active claims are structurally out of reach of every trim here.
PROTECTED_SOURCE_KINDS = frozenset(
    {"text", "markdown", "md", "pdf", "docx", "code", "html", "htm",
     "chatgpt-export", "claude-export", "image", "audio", "graph-community"}
)
SCREEN_EVENTS_MAX_AGE_DAYS = 30
RESOLVED_CANDIDATE_MAX_AGE_DAYS = 14
SUPERSEDED_CLAIM_MAX_AGE_DAYS = 30
AMBIENT_SUMMARY_MAX_AGE_DAYS = 90


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
        # Trim derived/noise data (never user content — see PROTECTED_SOURCE_KINDS).
        out["screen_events_trimmed"] = _trim_screen_memory_events(conn, now=now)
        out["candidates_trimmed"] = _trim_resolved_candidates(conn, now=now)
        out["claims_trimmed"] = _trim_superseded_claims(conn, now=now)
        out["ambient_summaries_pruned"] = _prune_old_ambient_summaries(conn, now=now)
        # L4: refresh community summaries when the graph has changed (cheap signature gate).
        out["communities"] = _maybe_build_communities(conn, data_dir)
    except Exception:
        log.exception("memory_lifecycle failed")
        out["error"] = True
    meta_set(conn, _META_LAST_RUN, str(now))
    out["finished_at"] = time.time()
    return out


_META_COMMUNITY_SIG = "memory_lifecycle_community_sig"


def _maybe_build_communities(conn, data_dir: Path) -> Dict[str, Any]:
    """Rebuild L4 community summaries only when the graph changed since last build."""
    try:
        from gemini_client import gemini_configured

        if not gemini_configured(data_dir):
            return {"skipped": "no_gemini"}
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), 0) AS mx FROM graph_nodes "
            "WHERE status NOT IN ('scaffold', 'stub')"
        ).fetchone()
        edges = conn.execute("SELECT COUNT(*) AS n FROM graph_edges").fetchone()
        sig = f"{int(row['n'])}:{float(row['mx']):.0f}:{int(edges['n'])}"
        if meta_get(conn, _META_COMMUNITY_SIG) == sig:
            return {"skipped": "unchanged"}
        from graph_community import build_communities  # lazy: graspologic optional

        result = build_communities(conn, data_dir)
        meta_set(conn, _META_COMMUNITY_SIG, sig)
        return result
    except Exception:
        log.debug("community rebuild skipped", exc_info=True)
        return {"skipped": "error"}


def _trim_screen_memory_events(conn, *, now: float) -> int:
    """Raw screen events are derived signal (consolidated into ambient-summary chunks)."""
    cutoff = now - SCREEN_EVENTS_MAX_AGE_DAYS * 86400.0
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM screen_memory_events WHERE occurred_at < ?", (cutoff,)
    ).fetchone()
    n = int(cur["n"]) if cur else 0
    if n:
        conn.execute("DELETE FROM screen_memory_events WHERE occurred_at < ?", (cutoff,))
    return n


def _trim_resolved_candidates(conn, *, now: float) -> int:
    """Graph candidates already resolved by the user are done — drop the stale ones."""
    cutoff = now - RESOLVED_CANDIDATE_MAX_AGE_DAYS * 86400.0
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM graph_candidates "
        "WHERE status IN ('approved', 'rejected', 'dismissed', 'merged') "
        "AND COALESCE(resolved_at, updated_at) < ?",
        (cutoff,),
    ).fetchone()
    n = int(cur["n"]) if cur else 0
    if n:
        conn.execute(
            "DELETE FROM graph_candidates "
            "WHERE status IN ('approved', 'rejected', 'dismissed', 'merged') "
            "AND COALESCE(resolved_at, updated_at) < ?",
            (cutoff,),
        )
    return n


def _trim_superseded_claims(conn, *, now: float) -> int:
    """Superseded/rejected identity claims are history; active claims are PROTECTED."""
    cutoff = now - SUPERSEDED_CLAIM_MAX_AGE_DAYS * 86400.0
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM identity_claims "
        "WHERE status IN ('superseded', 'rejected') AND updated_at < ?",
        (cutoff,),
    ).fetchone()
    n = int(cur["n"]) if cur else 0
    if n:
        conn.execute(
            "DELETE FROM identity_claims "
            "WHERE status IN ('superseded', 'rejected') AND updated_at < ?",
            (cutoff,),
        )
    return n


def _prune_old_ambient_summaries(conn, *, now: float) -> int:
    """ambient-summary sources are derived rollups; drop ones past retention."""
    cutoff = now - AMBIENT_SUMMARY_MAX_AGE_DAYS * 86400.0
    rows = conn.execute(
        "SELECT source_id, kind FROM sources WHERE kind = 'ambient-summary' AND mtime < ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0
    from store import delete_source

    n = 0
    for r in rows:
        if r["kind"] in PROTECTED_SOURCE_KINDS:  # invariant guard; never true here
            continue
        try:
            delete_source(conn, r["source_id"])
            n += 1
        except Exception:
            log.debug("failed to prune ambient-summary %s", r["source_id"])
    return n


def lifecycle_status(conn) -> Dict[str, Any]:
    """Counts per table + retention windows, for /maintenance/lifecycle-status."""
    def _count(sql: str) -> int:
        r = conn.execute(sql).fetchone()
        return int(r[0]) if r else 0

    return {
        "last_run": meta_get(conn, _META_LAST_RUN),
        "counts": {
            "chunks": _count("SELECT COUNT(*) FROM chunks"),
            "ambient_events": _count("SELECT COUNT(*) FROM ambient_events"),
            "screen_memory_events": _count("SELECT COUNT(*) FROM screen_memory_events"),
            "graph_candidates_open": _count("SELECT COUNT(*) FROM graph_candidates WHERE status='open'"),
            "graph_candidates_resolved": _count("SELECT COUNT(*) FROM graph_candidates WHERE status!='open'"),
            "identity_claims_active": _count("SELECT COUNT(*) FROM identity_claims WHERE status='active'"),
            "wiki_pages": _count("SELECT COUNT(*) FROM wiki_pages"),
        },
        "retention_days": {
            "ambient_events": AMBIENT_EVENTS_MAX_AGE_DAYS,
            "ambient_ax_sources": AMBIENT_AX_SOURCE_MAX_AGE_DAYS,
            "screen_memory_events": SCREEN_EVENTS_MAX_AGE_DAYS,
            "resolved_candidates": RESOLVED_CANDIDATE_MAX_AGE_DAYS,
            "superseded_claims": SUPERSEDED_CLAIM_MAX_AGE_DAYS,
            "ambient_summaries": AMBIENT_SUMMARY_MAX_AGE_DAYS,
            "hot_to_warm": HOT_TO_WARM_DAYS,
            "warm_to_cold": WARM_TO_COLD_DAYS,
        },
        "protected_kinds": sorted(PROTECTED_SOURCE_KINDS),
    }


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
