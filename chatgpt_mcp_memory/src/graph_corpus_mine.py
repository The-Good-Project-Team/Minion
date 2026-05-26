"""LLM-powered corpus graph mining — ongoing graph intelligence when Gemini is configured."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from store import meta_get, meta_set

log = logging.getLogger(__name__)

_META_STATS = "graph_mine_stats"

# One-time-ish scaffold buckets (family, birthplace, employers) — mined first when empty.
DURABLE_TARGETS: Tuple[Dict[str, Any], ...] = (
    {
        "parent_node_id": "scaffold-people-family",
        "node_kind": "person",
        "bucket_label": "Family",
        "hint": "family members spouse partner parents siblings children",
        "mining_queries": [
            "my family spouse partner wife husband children parents siblings",
            "family members who are related to me",
            "mother father brother sister son daughter",
        ],
        "stability": "core",
    },
    {
        "parent_node_id": "scaffold-places-home",
        "node_kind": "place",
        "bucket_label": "Home",
        "hint": "where I live hometown birthplace grew up",
        "mining_queries": [
            "where I live home address city hometown",
            "where I was born grew up childhood home",
            "primary residence location",
        ],
        "stability": "core",
    },
    {
        "parent_node_id": "scaffold-work-companies",
        "node_kind": "organization",
        "bucket_label": "Companies",
        "hint": "employer company where I work",
        "mining_queries": [
            "my employer company where I work job",
            "workplace organization I work for",
            "company name title role employment",
        ],
        "stability": "core",
    },
)

DEFAULT_MAX_CALLS_PER_DAY = 96
DEFAULT_MAX_CALLS_AMBIENT_TICK = 2
DEFAULT_MAX_CALLS_42_TICK = 4


def graph_mine_enabled(data_dir: Optional[Path]) -> bool:
    if not data_dir:
        return True
    try:
        from gemini_client import gemini_configured
        from settings import load_settings

        if not gemini_configured(data_dir):
            return False
        settings = load_settings(Path(data_dir))
        return settings.get("graph_mine_enabled", True) is not False
    except Exception:
        return False


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_stats(conn) -> Dict[str, Any]:
    raw = meta_get(conn, _META_STATS)
    if not raw:
        return {"day": _today_key(), "calls": 0, "filled": 0, "last_tick": 0.0}
    try:
        stats = json.loads(raw)
        if stats.get("day") != _today_key():
            stats = {"day": _today_key(), "calls": 0, "filled": 0, "last_tick": 0.0}
        return stats
    except json.JSONDecodeError:
        return {"day": _today_key(), "calls": 0, "filled": 0, "last_tick": 0.0}


def _save_stats(conn, stats: Dict[str, Any]) -> None:
    meta_set(conn, _META_STATS, json.dumps(stats, ensure_ascii=False))


def mine_limits(data_dir: Optional[Path]) -> Dict[str, int]:
    if not data_dir:
        return {
            "max_per_day": DEFAULT_MAX_CALLS_PER_DAY,
            "max_ambient_tick": DEFAULT_MAX_CALLS_AMBIENT_TICK,
            "max_42_tick": DEFAULT_MAX_CALLS_42_TICK,
        }
    try:
        from settings import load_settings

        s = load_settings(Path(data_dir))
        return {
            "max_per_day": int(s.get("graph_mine_max_calls_per_day") or DEFAULT_MAX_CALLS_PER_DAY),
            "max_ambient_tick": int(
                s.get("graph_mine_max_calls_per_tick") or DEFAULT_MAX_CALLS_AMBIENT_TICK
            ),
            "max_42_tick": int(
                s.get("graph_mine_42_max_calls_per_tick") or DEFAULT_MAX_CALLS_42_TICK
            ),
        }
    except Exception:
        return {
            "max_per_day": DEFAULT_MAX_CALLS_PER_DAY,
            "max_ambient_tick": DEFAULT_MAX_CALLS_AMBIENT_TICK,
            "max_42_tick": DEFAULT_MAX_CALLS_42_TICK,
        }


def graph_mine_status(conn, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    stats = _load_stats(conn)
    limits = mine_limits(data_dir)
    configured = False
    if data_dir:
        try:
            from gemini_client import gemini_configured

            configured = gemini_configured(data_dir)
        except Exception:
            configured = False
    return {
        "enabled": graph_mine_enabled(data_dir),
        "gemini_configured": configured,
        "calls_today": int(stats.get("calls") or 0),
        "filled_today": int(stats.get("filled") or 0),
        "max_calls_per_day": limits["max_per_day"],
        "last_tick": float(stats.get("last_tick") or 0),
    }


def _bucket_empty(conn, parent_id: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM graph_nodes "
        "WHERE parent_node_id=? AND status NOT IN ('scaffold', 'stub')",
        (parent_id,),
    ).fetchone()
    return row is not None and int(row["c"]) == 0


def _me_profile_empty(conn) -> bool:
    row = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id='scaffold-me'"
    ).fetchone()
    if not row:
        return True
    raw = str(row["summary"] or "").strip()
    if not raw or raw in ("{}", "Me", "You"):
        return True
    if raw.startswith("{"):
        try:
            meta = json.loads(raw)
            return not str(meta.get("user_profile") or meta.get("user_note") or "").strip()
        except json.JSONDecodeError:
            return len(raw) < 20
    return len(raw) < 20


def pick_mining_targets(
    conn,
    data_dir: Optional[Path],
    *,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """Ordered gaps for LLM corpus mining: core profile → durable buckets → gaps → enrich."""
    targets: List[Dict[str, Any]] = []

    if _me_profile_empty(conn):
        targets.append(
            {
                "gap_type": "me_profile",
                "label": "Me",
                "subject_id": "scaffold-me",
                "mining_kind": "durable",
                "stability": "core",
                "mining_queries": [
                    "about me biography background where born grew up",
                    "my name role title what I do professionally",
                    "personal facts hometown family context about myself",
                ],
            }
        )

    for spec in DURABLE_TARGETS:
        if not _bucket_empty(conn, str(spec["parent_node_id"])):
            continue
        targets.append(
            {
                "gap_type": "bucket",
                "parent_node_id": spec["parent_node_id"],
                "node_kind": spec["node_kind"],
                "bucket_label": spec["bucket_label"],
                "hint": spec["hint"],
                "mining_kind": "durable",
                "stability": spec.get("stability", "core"),
                "mining_queries": list(spec.get("mining_queries") or []),
            }
        )

    try:
        from graph_fill import pick_next_gap

        gap = pick_next_gap(conn, data_dir)
        if gap:
            g = dict(gap)
            g["mining_kind"] = "ongoing"
            g["stability"] = "active"
            targets.append(g)
    except Exception:
        pass

    rows = conn.execute(
        "SELECT node_id, node_kind, title, confidence, summary FROM graph_nodes "
        "WHERE status NOT IN ('scaffold', 'stub') AND node_id != 'scaffold-me' "
        "AND (confidence < 0.72 OR summary IS NULL OR summary='' OR summary='{}') "
        "ORDER BY updated_at ASC LIMIT ?",
        (max(limit, 3),),
    ).fetchall()
    for row in rows:
        targets.append(
            {
                "gap_type": "enrich",
                "subject_id": str(row["node_id"]),
                "node_kind": str(row["node_kind"]),
                "label": str(row["title"] or "node"),
                "mining_kind": "ongoing",
                "stability": "active",
                "mining_queries": [
                    str(row["title"] or ""),
                    f"who is {row['title']}",
                    f"about {row['title']} relationship role context",
                    f"{row['title']} project work updates",
                ],
            }
        )

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for t in targets:
        key = f"{t.get('gap_type')}:{t.get('subject_id') or t.get('parent_node_id') or t.get('label')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
        if len(deduped) >= limit:
            break
    return deduped


def run_graph_mine_tick(
    conn,
    data_dir: Optional[Path],
    *,
    max_llm_calls: int = 2,
    source: str = "ambient",
) -> Dict[str, Any]:
    """
    Spend up to max_llm_calls Gemini proposals applying validated graph writes.
    Core product loop: a few dollars of graph intelligence, continuously.
    """
    if not graph_mine_enabled(data_dir):
        return {"status": "disabled", "calls": 0, "filled": 0}

    limits = mine_limits(data_dir)
    stats = _load_stats(conn)
    remaining_day = max(0, limits["max_per_day"] - int(stats.get("calls") or 0))
    if remaining_day <= 0:
        return {"status": "daily_budget", "calls": 0, "filled": 0, "remaining_day": 0}

    budget = min(max_llm_calls, remaining_day)
    if budget <= 0:
        return {"status": "no_budget", "calls": 0, "filled": 0}

    from forty_two_infer import try_fill_gap_from_corpus

    filled = 0
    calls = 0
    deltas: List[str] = []
    last_status = "idle"

    for gap in pick_mining_targets(conn, data_dir, limit=budget + 2):
        if calls >= budget:
            break
        mining_kind = str(gap.get("mining_kind") or "ongoing")
        stability = str(gap.get("stability") or "active")
        result = try_fill_gap_from_corpus(
            conn,
            gap,
            data_dir=data_dir,
            mining_kind=mining_kind,
            stability=stability,
        )
        calls += 1
        st = result.get("status")
        last_status = st or "unknown"
        if st == "filled":
            filled += 1
            deltas.extend(result.get("deltas") or [])
            continue
        if st == "needs_question":
            break
        if st == "skipped" and result.get("reason") == "no_evidence":
            continue

    stats["calls"] = int(stats.get("calls") or 0) + calls
    stats["filled"] = int(stats.get("filled") or 0) + filled
    stats["last_tick"] = time.time()
    stats["last_source"] = source
    _save_stats(conn, stats)

    if data_dir and filled:
        try:
            from graph_events import log_graph_event

            label = ", ".join((deltas or [])[:2]) or f"{filled} graph update(s)"
            log_graph_event(
                data_dir,
                f"**42** mined your notes → {label[:240]}",
                action="mine",
            )
        except Exception:
            log.debug("graph mine event log failed", exc_info=True)

    return {
        "status": last_status,
        "calls": calls,
        "filled": filled,
        "deltas": deltas,
        "remaining_day": max(0, limits["max_per_day"] - int(stats["calls"])),
    }
