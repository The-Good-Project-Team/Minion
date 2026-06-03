"""LLM-powered corpus graph mining — ongoing graph intelligence when Gemini is configured."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from store import meta_get, meta_set

log = logging.getLogger(__name__)

_META_STATS = "graph_mine_stats"
_META_LAST_PERIODIC = "graph_mine_last_periodic"
_META_LAST_QUERY = "graph_mine_last_query"
_META_QUERY_PREFIX = "graph_mine_query:"

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

DEFAULT_MAX_CALLS_PER_DAY = 48
DEFAULT_MAX_CALLS_AMBIENT_TICK = 6
DEFAULT_MAX_CALLS_LIBRARIAN_TICK = 6
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_PERIODIC_INTERVAL_SEC = 6 * 60 * 60
DEFAULT_QUERY_MIN_INTERVAL_SEC = 15 * 60
DEFAULT_QUERY_SAME_INTERVAL_SEC = 6 * 60 * 60
DEFAULT_QUERY_MAX_CALLS = 1


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
            "max_librarian_tick": DEFAULT_MAX_CALLS_LIBRARIAN_TICK,
        }
    try:
        from settings import load_settings

        s = load_settings(Path(data_dir))
        return {
            "max_per_day": int(s.get("graph_mine_max_calls_per_day") or DEFAULT_MAX_CALLS_PER_DAY),
            "max_ambient_tick": int(
                s.get("graph_mine_max_calls_per_tick") or DEFAULT_MAX_CALLS_AMBIENT_TICK
            ),
            "max_librarian_tick": int(
                s.get("graph_mine_librarian_max_calls_per_tick") or DEFAULT_MAX_CALLS_LIBRARIAN_TICK
            ),
        }
    except Exception:
        return {
            "max_per_day": DEFAULT_MAX_CALLS_PER_DAY,
            "max_ambient_tick": DEFAULT_MAX_CALLS_AMBIENT_TICK,
            "max_librarian_tick": DEFAULT_MAX_CALLS_LIBRARIAN_TICK,
        }


def _setting_value(data_dir: Optional[Path], key: str, default: Any) -> Any:
    if not data_dir:
        return default
    try:
        from settings import load_settings

        return load_settings(Path(data_dir)).get(key, default)
    except Exception:
        return default


def _setting_int(
    data_dir: Optional[Path],
    key: str,
    default: int,
    *,
    minimum: int = 0,
) -> int:
    try:
        return max(minimum, int(_setting_value(data_dir, key, default)))
    except Exception:
        return max(minimum, int(default))


def graph_mine_periodic_interval_sec(data_dir: Optional[Path]) -> int:
    raw = ""
    try:
        import os

        raw = os.environ.get("MINION_GRAPH_MINE_INTERVAL_SEC", "").strip()
    except Exception:
        raw = ""
    if raw:
        try:
            return max(60, int(float(raw)))
        except ValueError:
            pass
    return _setting_int(
        data_dir,
        "graph_mine_interval_sec",
        DEFAULT_PERIODIC_INTERVAL_SEC,
        minimum=60,
    )


def _query_mine_options(data_dir: Optional[Path]) -> Dict[str, Any]:
    return {
        "enabled": bool(_setting_value(data_dir, "graph_mine_on_query_enabled", True)),
        "min_interval_sec": _setting_int(
            data_dir,
            "graph_mine_query_min_interval_sec",
            DEFAULT_QUERY_MIN_INTERVAL_SEC,
            minimum=60,
        ),
        "same_query_interval_sec": _setting_int(
            data_dir,
            "graph_mine_query_same_query_interval_sec",
            DEFAULT_QUERY_SAME_INTERVAL_SEC,
            minimum=60,
        ),
        "max_calls": _setting_int(
            data_dir,
            "graph_mine_query_max_calls_per_tick",
            DEFAULT_QUERY_MAX_CALLS,
            minimum=0,
        ),
    }


def _meta_float(conn, key: str) -> float:
    try:
        return float(meta_get(conn, key) or 0)
    except Exception:
        return 0.0


def _query_key(query: str) -> str:
    words = [w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in query).split() if len(w) > 1]
    normalized = " ".join(words[:12])
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{_META_QUERY_PREFIX}{digest}"


def run_periodic_graph_mine_tick(
    conn,
    data_dir: Optional[Path],
    *,
    max_llm_calls: Optional[int] = None,
    source: str = "periodic",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Run graph mining at most once per configured periodic interval."""
    now_f = float(now or time.time())
    interval = graph_mine_periodic_interval_sec(data_dir)
    last = _meta_float(conn, _META_LAST_PERIODIC)
    if last and now_f - last < interval:
        return {
            "status": "deferred",
            "calls": 0,
            "filled": 0,
            "next_due_at": last + interval,
        }
    if max_llm_calls is None:
        max_llm_calls = mine_limits(data_dir).get("max_ambient_tick", DEFAULT_MAX_CALLS_AMBIENT_TICK)
    out = run_graph_mine_tick(conn, data_dir, max_llm_calls=max_llm_calls, source=source)
    meta_set(conn, _META_LAST_PERIODIC, str(now_f))
    return out


def maybe_run_query_graph_mine(
    conn,
    data_dir: Optional[Path],
    query: str,
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Tiny query-triggered graph mine with global and same-query debounces."""
    q = " ".join((query or "").split()).strip()
    opts = _query_mine_options(data_dir)
    if not opts["enabled"]:
        return {"status": "disabled", "calls": 0, "filled": 0}
    if len(q) < 3 or int(opts["max_calls"]) <= 0:
        return {"status": "skipped", "reason": "query_not_actionable", "calls": 0, "filled": 0}
    now_f = float(now or time.time())
    last_any = _meta_float(conn, _META_LAST_QUERY)
    if last_any and now_f - last_any < float(opts["min_interval_sec"]):
        return {"status": "deferred", "reason": "query_global_debounce", "calls": 0, "filled": 0}
    key = _query_key(q)
    last_same = _meta_float(conn, key)
    if last_same and now_f - last_same < float(opts["same_query_interval_sec"]):
        return {"status": "deferred", "reason": "same_query_debounce", "calls": 0, "filled": 0}

    out = run_graph_mine_tick(
        conn,
        data_dir,
        max_llm_calls=int(opts["max_calls"]),
        source="query",
    )
    meta_set(conn, _META_LAST_QUERY, str(now_f))
    meta_set(conn, key, str(now_f))
    return out


_bg_lock = threading.Lock()
_bg_running = False


def graph_mine_max_output_tokens(data_dir: Optional[Path]) -> int:
    return _setting_int(
        data_dir,
        "graph_mine_max_output_tokens",
        DEFAULT_MAX_OUTPUT_TOKENS,
        minimum=256,
    )


def schedule_background_graph_mine(
    data_dir: Path,
    *,
    query: str = "",
    conn_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Fire-and-forget graph mine on a daemon thread (cheap Flash model, never blocks search)."""
    global _bg_running
    data = Path(data_dir).expanduser().resolve()
    q = " ".join((query or "").split()).strip()
    if q:
        opts = _query_mine_options(data)
        if not opts["enabled"] or int(opts["max_calls"]) <= 0:
            return {"status": "disabled", "reason": "query_mine_off"}
        if len(q) < 3:
            return {"status": "skipped", "reason": "query_not_actionable"}

    with _bg_lock:
        if _bg_running:
            return {"status": "skipped", "reason": "already_running"}
        _bg_running = True

    def _worker() -> None:
        global _bg_running
        try:
            if conn_factory is not None:
                conn = conn_factory()
            else:
                from store import DB_FILENAME, connect

                conn = connect(data / DB_FILENAME)
            try:
                if q:
                    maybe_run_query_graph_mine(conn, data, q)
                else:
                    run_periodic_graph_mine_tick(conn, data)
                conn.commit()
            finally:
                if conn_factory is None:
                    conn.close()
        except Exception:
            log.exception("background graph mine failed")
        finally:
            with _bg_lock:
                _bg_running = False

    threading.Thread(
        target=_worker,
        name="minion-graph-mine",
        daemon=True,
    ).start()
    return {"status": "scheduled", "mode": "query" if q else "periodic"}


def graph_mine_status(conn, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    stats = _load_stats(conn)
    limits = mine_limits(data_dir)
    configured = False
    model = ""
    if data_dir:
        try:
            from gemini_client import gemini_configured, graph_mine_gemini_model

            configured = gemini_configured(data_dir)
            if configured:
                model = graph_mine_gemini_model(data_dir)
        except Exception:
            configured = False
    return {
        "enabled": graph_mine_enabled(data_dir),
        "gemini_configured": configured,
        "calls_today": int(stats.get("calls") or 0),
        "filled_today": int(stats.get("filled") or 0),
        "max_calls_per_day": limits["max_per_day"],
        "last_tick": float(stats.get("last_tick") or 0),
        "periodic_interval_sec": graph_mine_periodic_interval_sec(data_dir),
        "last_periodic_tick": _meta_float(conn, _META_LAST_PERIODIC),
        "last_query_tick": _meta_float(conn, _META_LAST_QUERY),
        "on_query_enabled": bool(_query_mine_options(data_dir).get("enabled")),
        "max_output_tokens": graph_mine_max_output_tokens(data_dir),
        "model": model,
        "background_running": _bg_running,
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

    from librarian_infer import try_fill_gap_from_corpus

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
            # One ambiguous gap should not stall the rest of the tick — record the
            # open question (already persisted by try_fill_gap_from_corpus) and move
            # on so other durable gaps still get a chance to fill this tick.
            continue
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
                f"**Librarian** mined your notes → {label[:240]}",
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


# ---------------------------------------------------------------------------
# Corpus-agnostic eager mining (the graph-on-ingest build)
#
# The scaffold drip above (DURABLE_TARGETS / run_graph_mine_tick) chases a fixed
# set of personal-identity buckets (family/home/employers) — useless for an
# arbitrary new user's corpus (a company site, research notes, a product).
# `mine_corpus_entities` instead samples evidence across the WHOLE corpus and
# asks the LLM for the general entities actually present, then applies them
# through the same validated librarian path. It is meant to run ONCE after a
# bulk ingest settles (no 15-min/daily gate), with the drip remaining as the
# ongoing identity top-up.
# ---------------------------------------------------------------------------


def _graph_counts(conn) -> Tuple[int, int]:
    n = conn.execute(
        "SELECT count(*) FROM graph_nodes WHERE status NOT IN ('scaffold','stub')"
    ).fetchone()[0]
    e = conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0]
    return int(n), int(e)


def _sample_corpus_chunks(
    conn, *, max_chunks: int, min_len: int = 80
) -> List[Dict[str, Any]]:
    """Sample chunks stratified round-robin across sources so coverage is even
    and independent of ingest order. Skips our own graph-derived sources."""
    rows = conn.execute(
        "SELECT c.chunk_id, c.text, s.path, s.source_id "
        "FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        "WHERE length(c.text) >= ? "
        "AND s.kind NOT IN ('graph-community', 'graph-fact') "
        "ORDER BY s.source_id, c.seq",
        (min_len,),
    ).fetchall()
    from collections import OrderedDict

    by_src: "OrderedDict[str, List[Any]]" = OrderedDict()
    for r in rows:
        by_src.setdefault(str(r["source_id"]), []).append(r)

    out: List[Any] = []
    idx = 0
    while len(out) < max_chunks and any(idx < len(v) for v in by_src.values()):
        for v in by_src.values():
            if idx < len(v):
                out.append(v[idx])
                if len(out) >= max_chunks:
                    break
        idx += 1
    return [
        {"chunk_id": str(r["chunk_id"]), "text": str(r["text"] or ""), "path": str(r["path"] or "")}
        for r in out
    ]


def mine_corpus_entities(
    conn,
    data_dir: Optional[Path] = None,
    *,
    max_chunks: int = 96,
    batch: int = 6,
    max_llm_calls: int = 16,
    build_communities_after: bool = True,
) -> Dict[str, Any]:
    """Corpus-agnostic eager graph build. Returns before/after counts + stats.

    Safe to call with no LLM configured (skips gracefully so dense+sparse still
    work). Idempotent-ish: re-running re-proposes; entity-resolution + node
    existence checks in apply_proposal dedupe against existing nodes.
    """
    from gemini_client import gemini_configured

    if not gemini_configured(data_dir):
        return {"status": "skipped", "reason": "no_llm", "calls": 0}

    from librarian_infer import apply_proposal, sanitize_proposal, validate_proposal
    from librarian_llm import propose_graph_actions_from_evidence

    samples = _sample_corpus_chunks(conn, max_chunks=max_chunks)
    if not samples:
        return {"status": "skipped", "reason": "no_chunks", "calls": 0}

    n0, e0 = _graph_counts(conn)
    gap = {"gap_type": "corpus_mine", "label": "corpus entities", "stability": "active"}
    calls = 0
    applied = 0
    deltas: List[str] = []

    for i in range(0, len(samples), batch):
        if calls >= max_llm_calls:
            break
        group = samples[i : i + batch]
        pack = {
            "hits": [
                {"chunk_id": s["chunk_id"], "score": 1.0, "path": s["path"], "text": s["text"][:500]}
                for s in group
            ],
            "evidence_refs": [f"chunk:{s['chunk_id']}" for s in group],
        }
        try:
            proposal, used = propose_graph_actions_from_evidence(
                conn, gap, pack, data_dir=data_dir, mining_kind="corpus"
            )
        except Exception:
            log.warning("corpus mine proposal failed", exc_info=True)
            continue
        calls += 1
        if not used or not proposal:
            continue
        proposal["stability"] = "active"
        sanitize_proposal(proposal)
        ok, reason = validate_proposal(proposal, gap, pack)
        if not ok:
            log.debug("corpus mine proposal rejected: %s", reason)
            continue
        try:
            result = apply_proposal(conn, gap, proposal, data_dir=data_dir)
        except Exception:
            log.warning("corpus mine apply failed", exc_info=True)
            continue
        if result.get("filled"):
            applied += 1
            deltas.extend(result.get("deltas") or [])

    communities_summarized: Optional[int] = None
    if build_communities_after and applied:
        try:
            from graph_community import build_communities

            res = build_communities(conn, data_dir)
            communities_summarized = int(res.get("summarized") or 0) if isinstance(res, dict) else None
        except Exception:
            log.warning("community build after corpus mine failed", exc_info=True)

    n1, e1 = _graph_counts(conn)
    out = {
        "status": "ok",
        "calls": calls,
        "applied": applied,
        "nodes_before": n0,
        "nodes_after": n1,
        "edges_before": e0,
        "edges_after": e1,
        "communities": communities_summarized,
        "deltas": deltas[:20],
    }
    if data_dir and applied:
        try:
            from graph_events import log_graph_event

            log_graph_event(
                data_dir,
                kind="corpus_mine",
                summary=f"Built graph from corpus: +{n1 - n0} entities, +{e1 - e0} links",
            )
        except Exception:
            log.debug("graph event log skipped", exc_info=True)
    return out


# Debounced one-shot scheduler for the corpus build. A bulk drop fires many
# per-file ingest events; resetting the timer on each call coalesces them into a
# SINGLE build after activity settles, instead of one LLM run per file.
_CORPUS_BUILD_DEBOUNCE_SEC = 20.0
_corpus_build_timer: Optional[threading.Timer] = None
_corpus_build_lock = threading.Lock()
_corpus_build_running = False


def schedule_corpus_graph_build(
    data_dir: Path,
    *,
    delay: float = _CORPUS_BUILD_DEBOUNCE_SEC,
    conn_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Debounced, fire-and-forget corpus graph build after ingest settles.

    No-op when graph mining is disabled or no LLM is configured (so dense+sparse
    ingest stays fast and free). Reuses `mine_corpus_entities`.
    """
    global _corpus_build_timer
    data = Path(data_dir).expanduser().resolve()
    if not graph_mine_enabled(data):
        return {"status": "disabled"}
    try:
        from gemini_client import gemini_configured

        if not gemini_configured(data):
            return {"status": "skipped", "reason": "no_llm"}
    except Exception:
        return {"status": "skipped", "reason": "no_llm"}

    def _run() -> None:
        global _corpus_build_running
        with _corpus_build_lock:
            if _corpus_build_running:
                return
            _corpus_build_running = True
        try:
            if conn_factory is not None:
                conn = conn_factory()
            else:
                from store import DB_FILENAME, connect

                conn = connect(data / DB_FILENAME)
            try:
                res = mine_corpus_entities(conn, data)
                conn.commit()
                log.info("corpus graph build complete: %s", res)
            finally:
                if conn_factory is None:
                    conn.close()
        except Exception:
            log.exception("corpus graph build failed")
        finally:
            with _corpus_build_lock:
                _corpus_build_running = False

    with _corpus_build_lock:
        if _corpus_build_timer is not None:
            _corpus_build_timer.cancel()
        _corpus_build_timer = threading.Timer(delay, _run)
        _corpus_build_timer.daemon = True
        _corpus_build_timer.start()
    return {"status": "scheduled", "delay": delay}
