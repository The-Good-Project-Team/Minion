"""Parallel multi-lane retrieval: dense ∥ sparse ∥ graph, fused with weighted RRF,
then an optional cross-encoder rerank.

This is the core relevance path behind `ask_minion`. The three lanes read
independent tables (vec_chunks / fts_chunks / graph_*), so we fan them out across
a thread pool — each lane gets its own SQLite reader connection (WAL allows
concurrent readers). Falls back to in-line serial execution when no connection
factory is supplied.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Optional

import numpy as np

from retrieval_bias import rrf_fuse_many
from store import Hit, fts_available, keyword_search
from store import search as dense_search

log = logging.getLogger(__name__)

ConnFactory = Callable[[], sqlite3.Connection]

# Lane fusion weights. Semantic leads (cosine is the strongest single signal),
# graph facts are trusted, sparse keyword backstops rare proper-noun terms.
_W_SEMANTIC = 1.5
_W_GRAPH = 1.2
_W_SPARSE = 1.0

# Cross-encoder reranker (lazy singleton; fastembed reuses the existing dep).
_RERANK_MODEL_NAME = os.environ.get(
    "MINION_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2"
)
_reranker: Any = None
_reranker_failed = False


def rerank_enabled() -> bool:
    """On by default; MINION_DISABLE_RERANK=1 is the escape hatch."""
    return os.environ.get("MINION_DISABLE_RERANK", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    )


def _get_reranker():
    """Lazy-load the fastembed TextCrossEncoder. Returns None if unavailable."""
    global _reranker, _reranker_failed
    if _reranker is not None or _reranker_failed:
        return _reranker
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        from fastembed_cache import fastembed_cache_dir

        _reranker = TextCrossEncoder(
            model_name=_RERANK_MODEL_NAME, cache_dir=str(fastembed_cache_dir())
        )
    except Exception:
        # Old fastembed without rerank, model download failure, etc. — degrade
        # gracefully to fused order rather than failing the search.
        log.warning("cross-encoder reranker unavailable; using fused order", exc_info=True)
        _reranker_failed = True
        _reranker = None
    return _reranker


def rerank_hits(query: str, hits: List[Hit], *, top_n: int = 20, keep: int = 8) -> List[Hit]:
    """Cross-encoder rerank of the top-N fused candidates.

    Scores (query, hit.text) jointly and re-sorts. The cross-encoder score is
    stashed in Hit.meta['rerank_score']; Hit.score stays the original cosine so
    downstream telemetry/UI invariants hold. No-op (returns input) when disabled
    or the model can't load.
    """
    if not query or len(hits) <= 1 or not rerank_enabled():
        return hits[:keep] if keep else hits
    model = _get_reranker()
    if model is None:
        return hits[:keep] if keep else hits
    head = hits[:top_n]
    tail = hits[top_n:]
    try:
        scores = list(model.rerank(query, [h.text for h in head]))
    except Exception:
        log.warning("rerank scoring failed; using fused order", exc_info=True)
        return hits[:keep] if keep else hits
    for h, s in zip(head, scores):
        h.meta["rerank_score"] = float(s)
    head_sorted = sorted(head, key=lambda h: h.meta.get("rerank_score", 0.0), reverse=True)
    fused = [*head_sorted, *tail]
    return fused[:keep] if keep else fused


def _graph_lane(conn: sqlite3.Connection, qvec: np.ndarray, query: str, *, top_k: int, **kw: Any) -> List[Hit]:
    """First-class graph lane: graph facts + chunks scoped to matched nodes."""
    from graph_retrieval import (
        graph_fact_hits,
        graph_match_node_ids,
        linked_source_paths,
    )

    node_ids = graph_match_node_ids(conn, query)
    if not node_ids:
        return []
    hits: List[Hit] = list(graph_fact_hits(conn, node_ids, limit=min(3, top_k)))
    seen = {h.chunk_id for h in hits}
    # Scope dense search to the matched nodes' linked sources. Drop any caller
    # path_glob so we don't pass it twice; the node scope replaces it here.
    scoped_kw = {k: v for k, v in kw.items() if k != "path_glob"}
    for pglob in linked_source_paths(conn, node_ids)[:5]:
        try:
            scoped = dense_search(conn, qvec, top_k=top_k, path_glob=f"*{pglob}*", **scoped_kw)
        except Exception:
            continue
        for h in scoped:
            if h.chunk_id not in seen:
                hits.append(h)
                seen.add(h.chunk_id)
    return hits[:top_k]


def search_fused(
    conn: sqlite3.Connection,
    query: str,
    qvec: np.ndarray,
    *,
    top_k: int,
    conn_factory: Optional[ConnFactory] = None,
    rerank: bool = True,
    rerank_top_n: int = 60,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    since: Optional[float] = None,
    before: Optional[float] = None,
    after: Optional[float] = None,
    role: Optional[str] = None,
) -> List[Hit]:
    """Run dense ∥ sparse ∥ graph in parallel, RRF-fuse, then optionally rerank.

    `conn_factory` mints a fresh reader connection per lane so the lanes truly
    overlap. Without it the lanes run serially on `conn` (still correct).
    """
    dense_filters = dict(kind=kind, path_glob=path_glob, since=since, before=before, role=role)
    kw_filters = dict(kind=kind, path_glob=path_glob, before=before, after=after, role=role)
    has_fts = fts_available(conn)
    # Candidate pool for the lanes: when reranking, fetch enough per lane that the
    # cross-encoder has real recall to work with (a small pool caps the rerank — the
    # BEIR system eval showed reranking only ~20 candidates left ~0.025 nDCG@10 on the
    # floor vs reranking ~100). Final list is still trimmed to top_k.
    lane_k = max(top_k, rerank_top_n) if rerank else top_k

    def dense_lane(c: sqlite3.Connection) -> List[Hit]:
        return dense_search(c, qvec, top_k=lane_k, **dense_filters)

    def sparse_lane(c: sqlite3.Connection) -> List[Hit]:
        if not (query and has_fts):
            return []
        try:
            return keyword_search(c, query, top_k=lane_k, **kw_filters)
        except Exception:
            log.warning("sparse lane failed", exc_info=True)
            return []

    def graph_lane(c: sqlite3.Connection) -> List[Hit]:
        try:
            return _graph_lane(c, qvec, query, top_k=lane_k, **dense_filters)
        except Exception:
            log.warning("graph lane failed", exc_info=True)
            return []

    lanes = [dense_lane, sparse_lane, graph_lane]

    if conn_factory is not None:
        def run(fn: Callable[[sqlite3.Connection], List[Hit]]) -> List[Hit]:
            c = conn_factory()
            try:
                return fn(c)
            finally:
                try:
                    c.close()
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            dense_hits, sparse_hits, graph_hits = list(pool.map(run, lanes))
    else:
        dense_hits, sparse_hits, graph_hits = (dense_lane(conn), sparse_lane(conn), graph_lane(conn))

    # Order matters: semantic first so its cosine-scored Hit wins on overlap.
    fused = rrf_fuse_many(
        [
            (dense_hits, _W_SEMANTIC),
            (graph_hits, _W_GRAPH),
            (sparse_hits, _W_SPARSE),
        ]
    )
    if rerank:
        return rerank_hits(query, fused, top_n=rerank_top_n, keep=top_k)
    return fused[:top_k]
