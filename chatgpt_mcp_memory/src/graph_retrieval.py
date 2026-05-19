"""Graph-neighborhood scoped retrieval: match nodes → linked sources → search."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from store import Hit, search as store_search


def graph_match_node_ids(conn, query: str, *, limit: int = 5) -> List[str]:
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    tokens = [t for t in re.split(r"\W+", q) if len(t) >= 2][:6]
    if not tokens:
        return []
    rows = conn.execute(
        "SELECT node_id, title FROM graph_nodes WHERE node_kind IN ('person', 'place', 'group')"
    ).fetchall()
    scored: List[tuple[int, str]] = []
    for row in rows:
        label = str(row["title"] or "").lower()
        score = sum(1 for t in tokens if t in label)
        if score:
            scored.append((score, str(row["node_id"])))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [nid for _, nid in scored[:limit]]


def linked_source_paths(conn, node_ids: List[str]) -> List[str]:
    if not node_ids:
        return []
    paths: Set[str] = set()
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"SELECT source_refs_json FROM graph_edges WHERE from_node_id IN ({placeholders}) "
        f"OR to_node_id IN ({placeholders})",
        (*node_ids, *node_ids),
    ).fetchall()
    import json

    for row in rows:
        meta = json.loads(row["source_refs_json"] or "{}")
        p = meta.get("source_path") or meta.get("wiki_path")
        if p:
            paths.add(str(p))
    for nid in node_ids:
        wiki_rows = conn.execute(
            "SELECT page_id FROM wiki_pages WHERE title IN "
            "(SELECT title FROM graph_nodes WHERE node_id=?)",
            (nid,),
        ).fetchall()
        if wiki_rows:
            paths.add(f"wiki:{nid}")
    return list(paths)[:20]


def neighborhood_search(
    conn,
    qvec: List[float],
    query: str,
    *,
    top_k: int,
    **search_kw: Any,
) -> List[Hit]:
    """Prefer chunks linked to graph nodes mentioned in the query; fall back to global."""
    node_ids = graph_match_node_ids(conn, query)
    if not node_ids:
        return store_search(conn, qvec, top_k=top_k, **search_kw)
    paths = linked_source_paths(conn, node_ids)
    if not paths:
        return store_search(conn, qvec, top_k=top_k, **search_kw)
    scoped: List[Hit] = []
    for pglob in paths[:5]:
        try:
            scoped.extend(
                store_search(conn, qvec, top_k=top_k, path_glob=f"*{pglob}*", **search_kw)
            )
        except TypeError:
            scoped.extend(store_search(conn, qvec, top_k=top_k, **search_kw))
            break
    if len(scoped) >= top_k:
        return scoped[:top_k]
    global_hits = store_search(conn, qvec, top_k=top_k, **search_kw)
    seen = {h.chunk_id for h in scoped}
    for h in global_hits:
        if h.chunk_id not in seen:
            scoped.append(h)
        if len(scoped) >= top_k:
            break
    return scoped[:top_k]
