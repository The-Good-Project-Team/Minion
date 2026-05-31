"""Tests for Layer 4 graph community summaries.

The detection/grouping path is tested deterministically with NO LLM and NO
network (pure functions over an in-memory graph). The full build_communities
path -- which calls Gemini -- is gated behind gemini availability so the suite
runs offline.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from gemini_client import gemini_configured
from graph_community import (
    COMMUNITY_KIND,
    COMMUNITY_META_KEY,
    build_communities,
    build_node_graph,
    communities_from_conn,
    detect_communities,
    get_community_index,
    load_graph_edges,
    load_graph_nodes,
)
from store import connect, list_sources, meta_get


def _insert_node(conn, node_id: str, kind: str, title: str, summary: str = "", status: str = "active") -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, summary, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (node_id, kind, title, status, summary, now, now),
    )


def _insert_edge(conn, from_id: str, to_id: str, rel: str = "related") -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO graph_edges(edge_id, from_node_id, to_node_id, rel_kind, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (f"e-{from_id}-{to_id}-{rel}", from_id, to_id, rel, now),
    )


@pytest.fixture()
def two_cluster_db(tmp_path: Path):
    """A graph with two clearly separate communities plus one scaffold node
    (which must be excluded) and one isolated singleton."""
    db = tmp_path / "memory.db"
    conn = connect(db)
    # Cluster A: a1-a2-a3 fully connected.
    _insert_node(conn, "a1", "project", "Alpha One", "first alpha thing")
    _insert_node(conn, "a2", "project", "Alpha Two", "second alpha thing")
    _insert_node(conn, "a3", "organization", "Alpha Org", "alpha org")
    _insert_edge(conn, "a1", "a2", "related")
    _insert_edge(conn, "a2", "a3", "manages")
    _insert_edge(conn, "a1", "a3", "related")
    # Cluster B: b1-b2.
    _insert_node(conn, "b1", "topic", "Beta One", "first beta thing")
    _insert_node(conn, "b2", "topic", "Beta Two", "second beta thing")
    _insert_edge(conn, "b1", "b2", "related")
    # Singleton (no edges to anyone in scope).
    _insert_node(conn, "s1", "person", "Solo", "isolated node")
    # Scaffold node must be excluded from detection. (connect() already seeds a
    # 'scaffold-me' node, so we add our own distinct scaffold id here.)
    _insert_node(conn, "scaffold-extra", "person", "Scaffold", "", status="scaffold")
    _insert_edge(conn, "a1", "scaffold-extra", "owns")
    conn.commit()
    yield conn
    conn.close()


def test_load_excludes_scaffold(two_cluster_db) -> None:
    nodes = load_graph_nodes(two_cluster_db)
    assert "scaffold-me" not in nodes
    assert set(nodes) == {"a1", "a2", "a3", "b1", "b2", "s1"}
    # Edges to scaffold nodes are dropped (both endpoints must be in scope).
    edges = load_graph_edges(two_cluster_db, list(nodes))
    endpoints = {e[0] for e in edges} | {e[1] for e in edges}
    assert "scaffold-me" not in endpoints


def test_detect_communities_finds_two_clusters(two_cluster_db) -> None:
    communities, nodes, edges = communities_from_conn(two_cluster_db)
    # Map node -> community id.
    comm_of = {n: cid for cid, members in communities.items() for n in members}
    # Alpha members share a community; Beta members share a (different) community.
    assert comm_of["a1"] == comm_of["a2"] == comm_of["a3"]
    assert comm_of["b1"] == comm_of["b2"]
    assert comm_of["a1"] != comm_of["b1"]
    # Singleton is its own community, separate from both clusters.
    assert comm_of["s1"] != comm_of["a1"]
    assert comm_of["s1"] != comm_of["b1"]
    # At least two communities of size >= 2 were found.
    big = [m for m in communities.values() if len(m) >= 2]
    assert len(big) >= 2


def test_detect_communities_empty_graph() -> None:
    g = build_node_graph([], [])
    assert detect_communities(g) == {}


def test_detect_communities_single_node_no_edges() -> None:
    # Trivial graph: Leiden would error; we must fall back gracefully.
    g = build_node_graph(["lonely"], [])
    communities = detect_communities(g)
    assert communities == {0: ["lonely"]}


def test_detect_communities_disconnected_no_edges() -> None:
    g = build_node_graph(["p", "q", "r"], [])
    communities = detect_communities(g)
    # Each isolated node becomes its own community.
    assert len(communities) == 3
    flattened = sorted(n for members in communities.values() for n in members)
    assert flattened == ["p", "q", "r"]


@pytest.mark.skipif(not gemini_configured(), reason="gemini not configured; offline run")
def test_build_communities_live(two_cluster_db) -> None:
    os.environ["MINION_DETERMINISTIC_EMBEDDINGS"] = "1"
    try:
        result = build_communities(two_cluster_db, data_dir=None)
    finally:
        os.environ.pop("MINION_DETERMINISTIC_EMBEDDINGS", None)
    assert result["communities"] >= 2
    assert result["summarized"] >= 2
    sources = list_sources(two_cluster_db, kind=COMMUNITY_KIND)
    assert len(sources) == result["summarized"]
    index = get_community_index(two_cluster_db)
    assert len(index) == result["summarized"]
    assert meta_get(two_cluster_db, COMMUNITY_META_KEY)
