"""Graph-neighborhood scoped retrieval: match nodes → linked sources → search."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, List, Set

from store import Hit, search as store_search


def graph_match_node_ids(conn, query: str, *, limit: int = 5) -> List[str]:
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    tokens = [t for t in re.split(r"\W+", q) if len(t) >= 2][:6]
    if not tokens:
        return []
    rows = conn.execute(
        "SELECT node_id, title, summary FROM graph_nodes WHERE status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    scored: List[tuple[int, str]] = []
    for row in rows:
        label = f"{row['title'] or ''} {row['summary'] or ''}".lower()
        score = sum(1 for t in tokens if t in label)
        if score:
            scored.append((score, str(row["node_id"])))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [nid for _, nid in scored[:limit]]


def traverse_graph(
    conn,
    *,
    query: str | None = None,
    node_id: str | None = None,
    hops: int = 2,
    max_nodes: int = 40,
) -> dict:
    """Walk the knowledge graph from an entity (by name via `query`, or by
    `node_id`) outward up to `hops`, returning the local subgraph.

    Unlike get_node (one hop from a known pointer), this resolves a start by
    NAME and follows typed edges multiple hops, so an agent can answer
    relationship/multi-hop questions ("how is X connected to Y", "what's around
    Z") without already holding a node pointer.
    """
    hops = max(1, min(4, int(hops)))
    max_nodes = max(1, min(200, int(max_nodes)))

    starts: List[str] = []
    if node_id:
        nid = node_id[len("graph:"):] if node_id.startswith("graph:") else node_id
        if nid.strip():
            starts = [nid.strip()]
    elif query:
        starts = graph_match_node_ids(conn, query, limit=5)

    def _node(nid: str):
        return conn.execute(
            "SELECT node_id, node_kind, title, summary, confidence FROM graph_nodes WHERE node_id=?",
            (nid,),
        ).fetchone()

    nodes: dict = {}
    edges: List[dict] = []
    seen_edges: Set[tuple] = set()
    frontier = list(dict.fromkeys(starts))
    depth = 0
    truncated = False
    while frontier and depth <= hops and len(nodes) < max_nodes:
        nxt: List[str] = []
        for nid in frontier:
            if nid in nodes:
                continue
            r = _node(nid)
            if r is None:
                continue
            nodes[nid] = {
                "node_id": nid,
                "kind": str(r["node_kind"] or ""),
                "label": str(r["title"] or ""),
                "summary": (str(r["summary"] or ""))[:280],
                "hop": depth,
            }
            if len(nodes) >= max_nodes:
                truncated = True
                break
            if depth == hops:
                continue  # collect this node but don't expand past the hop budget
            for e in conn.execute(
                "SELECT from_node_id, to_node_id, rel_kind FROM graph_edges "
                "WHERE from_node_id=? OR to_node_id=?",
                (nid, nid),
            ).fetchall():
                out = str(e["from_node_id"]) == nid
                other = str(e["to_node_id"] if out else e["from_node_id"])
                ekey = (str(e["from_node_id"]), str(e["to_node_id"]), str(e["rel_kind"] or ""))
                if ekey not in seen_edges:
                    seen_edges.add(ekey)
                    edges.append({
                        "from": ekey[0], "to": ekey[1], "rel": ekey[2],
                    })
                nxt.append(other)
        frontier = nxt
        depth += 1

    return {
        "start_nodes": starts,
        "hops": hops,
        "nodes": list(nodes.values()),
        "edges": edges,
        "truncated": truncated or len(nodes) >= max_nodes,
    }


def linked_source_paths(conn, node_ids: List[str]) -> List[str]:
    if not node_ids:
        return []
    paths: Set[str] = set()
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"SELECT source_refs_json FROM graph_nodes WHERE node_id IN ({placeholders}) "
        f"UNION ALL "
        f"SELECT source_refs_json FROM graph_edges WHERE from_node_id IN ({placeholders}) "
        f"OR to_node_id IN ({placeholders})",
        (*node_ids, *node_ids, *node_ids),
    ).fetchall()
    for row in rows:
        paths.update(_source_paths_from_refs(_json_refs(row["source_refs_json"])))
    for nid in node_ids:
        wiki_rows = conn.execute(
            "SELECT page_id FROM wiki_pages WHERE title IN "
            "(SELECT title FROM graph_nodes WHERE node_id=?)",
            (nid,),
        ).fetchall()
        if wiki_rows:
            paths.add(f"wiki:{nid}")
    return list(paths)[:20]


def graph_fact_hits(conn, node_ids: List[str], *, limit: int = 5) -> List[Hit]:
    if not node_ids:
        return []
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"SELECT node_id, node_kind, title, summary, source_refs_json, confidence, updated_at "
        f"FROM graph_nodes WHERE node_id IN ({placeholders}) "
        f"AND status NOT IN ('scaffold', 'stub')",
        tuple(node_ids),
    ).fetchall()
    order = {nid: i for i, nid in enumerate(node_ids)}
    hits: List[Hit] = []
    for row in rows:
        refs = _json_refs(row["source_refs_json"])
        nid = str(row["node_id"])
        hits.append(
            Hit(
                chunk_id=f"graph:{nid}",
                score=max(0.5, float(row["confidence"] or 0.65)),
                text=_graph_fact_text(row, refs),
                role="graph",
                source_id=f"graph:{nid}",
                path=f"graph/{row['node_kind']}/{nid}",
                kind="graph-fact",
                mtime=float(row["updated_at"] or time.time()),
                meta={
                    "node_id": nid,
                    "node_kind": str(row["node_kind"] or ""),
                    "label": str(row["title"] or ""),
                    "source_refs": refs[:10],
                },
                source_meta={"source": "life_graph"},
                storage_tier="hot",
            )
        )
    hits.sort(key=lambda h: order.get(str(h.meta.get("node_id")), 999))
    return hits[:limit]


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
    graph_hits = graph_fact_hits(conn, node_ids, limit=min(3, top_k))
    paths = linked_source_paths(conn, node_ids)
    if not paths:
        global_hits = store_search(conn, qvec, top_k=max(0, top_k - len(graph_hits)), **search_kw)
        return [*graph_hits, *global_hits][:top_k]
    scoped: List[Hit] = []
    for pglob in paths[:5]:
        try:
            scoped.extend(
                store_search(conn, qvec, top_k=top_k, path_glob=f"*{pglob}*", **search_kw)
            )
        except TypeError:
            scoped.extend(store_search(conn, qvec, top_k=top_k, **search_kw))
            break
    merged: List[Hit] = list(graph_hits)
    seen = {h.chunk_id for h in merged}
    for h in scoped:
        if h.chunk_id not in seen:
            merged.append(h)
            seen.add(h.chunk_id)
    if len(merged) >= top_k:
        return merged[:top_k]
    global_hits = store_search(conn, qvec, top_k=top_k, **search_kw)
    for h in global_hits:
        if h.chunk_id not in seen:
            merged.append(h)
            seen.add(h.chunk_id)
        if len(merged) >= top_k:
            break
    return merged[:top_k]


def _json_refs(raw: Any) -> List[str]:
    try:
        val = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if str(x).strip()]
    if isinstance(val, dict):
        return [str(v) for k, v in val.items() if "path" in str(k).lower() and str(v).strip()]
    return []


def _source_paths_from_refs(refs: Iterable[str]) -> Set[str]:
    paths: Set[str] = set()
    for ref in refs:
        text = str(ref).strip()
        for prefix in ("source_path:", "source:", "path:"):
            if text.startswith(prefix):
                paths.add(text[len(prefix) :])
    return paths


def _graph_fact_text(row: Any, refs: List[str]) -> str:
    summary = str(row["summary"] or "").strip()
    if summary.startswith("{"):
        try:
            parsed = json.loads(summary)
            summary = " ".join(str(parsed.get(k) or "") for k in ("user_note", "relation_note", "recent_attention")).strip()
        except json.JSONDecodeError:
            pass
    bits = [f"Graph fact: {row['title']} ({row['node_kind']}).", summary]
    paths = sorted(_source_paths_from_refs(refs))
    if paths:
        bits.append("Linked evidence: " + ", ".join(paths[:3]))
    return "\n".join(b for b in bits if b)
