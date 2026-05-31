"""
Layer 4: community summaries / global index over the knowledge graph.

This module is self-contained and does NOT touch the retrieval hot path. It

  1. builds a networkx graph from `graph_nodes` / `graph_edges` (non-scaffold
     nodes only),
  2. partitions it into communities with graspologic's Leiden algorithm
     (`graspologic.partition.hierarchical_leiden` -- the same one Microsoft
     GraphRAG uses), falling back to connected components for trivial /
     disconnected graphs that Leiden refuses,
  3. asks Gemini for a one-paragraph, corpus-agnostic summary of each
     community (size >= 2), and
  4. persists each summary as a retrievable chunk via
     ``store.upsert_source(kind="graph-community", role="community_summary")``
     so the existing dense lane retrieves it with no hot-path changes.

Public entry point: ``build_communities(conn, data_dir)``.

The detection + grouping is factored into pure functions
(``build_node_graph`` / ``detect_communities``) so it can be unit-tested
deterministically without the LLM or any network access.

graspologic is MIT-licensed (Microsoft). networkx (its dependency) is BSD.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

import store

log = logging.getLogger("minion.graph_community")

# Sources we own are addressed under this path prefix; idempotent rebuilds wipe
# every source of this kind first, so the prefix is just for readability.
COMMUNITY_KIND = "graph-community"
COMMUNITY_ROLE = "community_summary"
COMMUNITY_PATH_PREFIX = "graph/community/"
# Meta key holding the JSON index {community_id: {members, size, path, ...}}.
COMMUNITY_META_KEY = "graph_community_index"

# Only communities with at least this many members get a summary; singletons
# carry no relational signal worth a dedicated global-index chunk.
MIN_COMMUNITY_SIZE = 2

# Cap members fed into one prompt so a giant community can't blow the context.
_MAX_MEMBERS_IN_PROMPT = 40
_MAX_EDGES_IN_PROMPT = 80


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_graph_nodes(conn) -> Dict[str, Dict[str, Any]]:
    """Return non-scaffold/non-stub nodes keyed by node_id."""
    rows = conn.execute(
        "SELECT node_id, node_kind, title, summary, status "
        "FROM graph_nodes WHERE status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        nid = str(r["node_id"])
        out[nid] = {
            "node_id": nid,
            "node_kind": str(r["node_kind"] or ""),
            "title": str(r["title"] or ""),
            "summary": str(r["summary"] or ""),
            "status": str(r["status"] or ""),
        }
    return out


def load_graph_edges(conn, node_ids: Sequence[str]) -> List[Tuple[str, str, str]]:
    """Return (from, to, rel_kind) edges where BOTH endpoints are in node_ids."""
    keep = set(node_ids)
    rows = conn.execute(
        "SELECT from_node_id, to_node_id, rel_kind FROM graph_edges"
    ).fetchall()
    edges: List[Tuple[str, str, str]] = []
    for r in rows:
        a = str(r["from_node_id"])
        b = str(r["to_node_id"])
        if a in keep and b in keep and a != b:
            edges.append((a, b, str(r["rel_kind"] or "related")))
    return edges


# ---------------------------------------------------------------------------
# Pure detection / grouping (no LLM, no DB)
# ---------------------------------------------------------------------------


def build_node_graph(node_ids: Sequence[str], edges: Sequence[Tuple[str, str, str]]):
    """Build an undirected networkx graph. Parallel edges between the same pair
    accumulate as integer weight so Leiden sees a stronger tie."""
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from([str(n) for n in node_ids])
    for a, b, _rel in edges:
        if g.has_edge(a, b):
            g[a][b]["weight"] = g[a][b].get("weight", 1.0) + 1.0
        else:
            g.add_edge(a, b, weight=1.0)
    return g


def _components_partition(graph) -> Dict[str, int]:
    """Fallback: each connected component is one community."""
    import networkx as nx

    mapping: Dict[str, int] = {}
    for cid, comp in enumerate(nx.connected_components(graph)):
        for node in comp:
            mapping[str(node)] = cid
    return mapping


def detect_communities(
    graph,
    *,
    max_cluster_size: int = 10,
    random_seed: int = 0xC0FFEE,
) -> Dict[int, List[str]]:
    """Partition `graph` into communities. Pure: no DB, no LLM.

    Uses ``graspologic.partition.hierarchical_leiden`` (Leiden), taking the
    final (leaf) cluster assignment for each node. Falls back to connected
    components when the graph is trivial/disconnected or graspologic raises
    (Leiden rejects empty / edgeless / single-node graphs).

    Returns {community_id: [node_id, ...]} with contiguous ids ordered by
    descending size; includes singletons -- the caller applies any size
    threshold.
    """
    n_nodes = graph.number_of_nodes()
    n_edges = graph.number_of_edges()
    if n_nodes == 0:
        return {}

    mapping: Optional[Dict[str, int]] = None
    # Leiden needs at least one edge and >1 node; otherwise it errors.
    if n_nodes > 1 and n_edges > 0:
        try:
            from graspologic.partition import hierarchical_leiden

            partition = hierarchical_leiden(
                graph,
                max_cluster_size=max_cluster_size,
                random_seed=random_seed,
                check_directed=False,
            )
            # hierarchical_leiden returns a list of HierarchicalCluster rows; the
            # leaf assignment (is_final_cluster) is the community we want.
            leaf: Dict[str, int] = {}
            for row in partition:
                if getattr(row, "is_final_cluster", True):
                    leaf[str(row.node)] = int(row.cluster)
            if leaf:
                mapping = leaf
        except Exception as e:  # pragma: no cover - exercised only on odd graphs
            log.warning("Leiden failed (%s); falling back to connected components", e)
            mapping = None

    if mapping is None:
        mapping = _components_partition(graph)

    # Ensure every node lands somewhere (Leiden can omit isolated nodes).
    next_id = (max(mapping.values()) + 1) if mapping else 0
    for node in graph.nodes():
        nid = str(node)
        if nid not in mapping:
            mapping[nid] = next_id
            next_id += 1

    groups: Dict[int, List[str]] = {}
    for node, cid in mapping.items():
        groups.setdefault(int(cid), []).append(str(node))
    # Stable, contiguous community ids ordered by descending size then id.
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {new_id: sorted(members) for new_id, (_old, members) in enumerate(ordered)}


def communities_from_conn(
    conn, **kwargs
) -> Tuple[Dict[int, List[str]], Dict[str, Dict[str, Any]], List[Tuple[str, str, str]]]:
    """Convenience: load graph from `conn` and detect. Returns
    (communities, nodes_by_id, edges). Pure read; no writes, no LLM."""
    nodes = load_graph_nodes(conn)
    edges = load_graph_edges(conn, list(nodes.keys()))
    graph = build_node_graph(list(nodes.keys()), edges)
    communities = detect_communities(graph, **kwargs)
    return communities, nodes, edges


# ---------------------------------------------------------------------------
# Summarization (LLM)
# ---------------------------------------------------------------------------


_SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "summary": {"type": "STRING"},
    },
    "required": ["summary"],
}

_SUMMARY_SYSTEM = (
    "You summarize a community of related entities from a personal knowledge "
    "graph. Given the member entities (titles, kinds, descriptions) and the "
    "relationships among them, write ONE concise paragraph (2-4 sentences) "
    "describing what ties this community together and what it is about. Be "
    "specific and grounded only in the provided members and relationships; do "
    "not invent facts. Also propose a short descriptive title (a few words). "
    "Respond as JSON with keys 'title' and 'summary'."
)


def _render_community_context(
    members: Sequence[str],
    nodes_by_id: Dict[str, Dict[str, Any]],
    edges: Sequence[Tuple[str, str, str]],
) -> str:
    member_set = set(members)
    lines: List[str] = ["MEMBERS:"]
    for nid in list(members)[:_MAX_MEMBERS_IN_PROMPT]:
        n = nodes_by_id.get(nid, {})
        title = n.get("title") or nid
        kind = n.get("node_kind") or "entity"
        summ = (n.get("summary") or "").strip()
        line = f"- {title} ({kind})"
        if summ:
            line += f": {summ}"
        lines.append(line)
    rel_lines: List[str] = []
    for a, b, rel in edges:
        if a in member_set and b in member_set:
            ta = nodes_by_id.get(a, {}).get("title") or a
            tb = nodes_by_id.get(b, {}).get("title") or b
            rel_lines.append(f"- {ta} --{rel}--> {tb}")
        if len(rel_lines) >= _MAX_EDGES_IN_PROMPT:
            break
    if rel_lines:
        lines.append("")
        lines.append("RELATIONSHIPS:")
        lines.extend(rel_lines)
    return "\n".join(lines)


def summarize_community(
    members: Sequence[str],
    nodes_by_id: Dict[str, Dict[str, Any]],
    edges: Sequence[Tuple[str, str, str]],
    *,
    data_dir: Optional[Path],
) -> Tuple[str, str]:
    """Return (title, summary) for one community via Gemini. Raises on LLM error
    (caller decides whether to skip). Corpus-agnostic prompt -- no hardcoded
    names."""
    from gemini_client import gemini_chat, graph_mine_gemini_model

    context = _render_community_context(members, nodes_by_id, edges)
    raw = gemini_chat(
        system=_SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": context}],
        data_dir=data_dir,
        model=graph_mine_gemini_model(data_dir) if data_dir else None,
        response_mime_type="application/json",
        response_schema=_SUMMARY_SCHEMA,
        max_output_tokens=400,
    )
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        obj = {"summary": str(raw).strip()}
    summary = str(obj.get("summary") or "").strip()
    title = str(obj.get("title") or "").strip()
    if not title:
        # Derive a fallback title from the largest member titles.
        titles = [nodes_by_id.get(m, {}).get("title") or m for m in list(members)[:3]]
        title = ", ".join(t for t in titles if t) or "Community"
    return title, summary


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _embed_texts(texts: List[str]) -> np.ndarray:
    """Embed with the same fastembed model the rest of the corpus uses
    (honors MINION_DETERMINISTIC_EMBEDDINGS for offline tests)."""
    from ingest import DEFAULT_MODEL, _embed, _get_model

    model = _get_model(DEFAULT_MODEL)
    return _embed(model, texts)


def _persist_community(
    conn,
    *,
    community_id: int,
    title: str,
    summary: str,
    members: Sequence[str],
    nodes_by_id: Dict[str, Dict[str, Any]],
) -> str:
    """Write one community summary as a retrievable source/chunk. Returns path."""
    path = f"{COMMUNITY_PATH_PREFIX}{community_id}"
    member_titles = [nodes_by_id.get(m, {}).get("title") or m for m in members]
    # The embedded text leads with the title + summary, then members, so the
    # dense lane can match either a topical query or a member-name query.
    text = f"{title}\n\n{summary}\n\nMembers: " + ", ".join(member_titles)
    embeddings = _embed_texts([text])
    source_meta = {
        "community_id": community_id,
        "title": title,
        "member_node_ids": list(members),
        "member_titles": member_titles,
        "size": len(members),
        "generated_at": time.time(),
    }
    chunk_meta = {
        "community_id": community_id,
        "member_node_ids": list(members),
        "kind": COMMUNITY_KIND,
    }
    store.upsert_source(
        conn,
        path=path,
        kind=COMMUNITY_KIND,
        sha256=f"community-{community_id}",
        mtime=time.time(),
        bytes_=len(text.encode("utf-8")),
        parser="graph_community",
        source_meta=source_meta,
        chunks=[(text, COMMUNITY_ROLE, chunk_meta)],
        embeddings=embeddings,
    )
    return path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_communities(conn, data_dir: Optional[Path] = None) -> Dict[str, int]:
    """Detect graph communities, summarize each (size >= 2) via Gemini, and
    persist the summaries as retrievable chunks.

    Idempotent: drops all prior ``kind="graph-community"`` sources first, so
    re-running fully replaces the previous community index.

    Returns {"communities": <count detected>, "summarized": <count persisted>}.
    """
    data_dir = Path(data_dir) if data_dir else None

    communities, nodes_by_id, edges = communities_from_conn(conn)

    # Idempotent rebuild: clear our prior sources before writing new ones.
    store.delete_sources_by_kind(conn, COMMUNITY_KIND)

    summarized = 0
    index: Dict[str, Dict[str, Any]] = {}
    for cid, members in communities.items():
        if len(members) < MIN_COMMUNITY_SIZE:
            continue
        try:
            title, summary = summarize_community(
                members, nodes_by_id, edges, data_dir=data_dir
            )
        except Exception as e:
            log.warning("community %s summary failed: %s", cid, e)
            continue
        if not summary:
            continue
        path = _persist_community(
            conn,
            community_id=cid,
            title=title,
            summary=summary,
            members=members,
            nodes_by_id=nodes_by_id,
        )
        index[str(cid)] = {
            "title": title,
            "members": list(members),
            "size": len(members),
            "path": path,
        }
        summarized += 1

    store.meta_set(conn, COMMUNITY_META_KEY, json.dumps(index, ensure_ascii=False))
    conn.commit()

    return {"communities": len(communities), "summarized": summarized}


def get_community_index(conn) -> Dict[str, Dict[str, Any]]:
    """Return the persisted community index (see COMMUNITY_META_KEY)."""
    raw = store.meta_get(conn, COMMUNITY_META_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}
