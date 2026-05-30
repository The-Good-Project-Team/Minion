"""L3 corpus-entity candidates: feed surfacing + confirm → graph node.

Covers the deterministic half of the flow (no LLM): a corpus_entity candidate
rendered into the feed and resolved into a real graph node. The Gemini extraction
call itself is integration-tested live, not here.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from activity_feed import _candidate_suggestions  # noqa: E402
from graph_fill import apply_graph_candidate_resolution  # noqa: E402
from store import connect, graph_candidate_create, graph_candidate_list  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "memory.db")
    try:
        yield c
    finally:
        c.close()


def _make_candidate(conn, label: str, kind: str) -> str:
    return graph_candidate_create(
        conn,
        candidate_type="corpus_entity",
        title=label,
        body_md=f"Is {label} yours?",
        payload={"label": label, "node_kind": kind, "evidence": "seen in corpus"},
        evidence_refs=["source_path:inbox/notes.md"],
        confidence=0.5,
        source="corpus_extract",
    )


def test_candidate_surfaces_in_feed_with_actions(conn) -> None:
    _make_candidate(conn, "Acme Corp", "organization")
    items = _candidate_suggestions(graph_candidate_list(conn, status="open"))
    assert len(items) == 1
    item = items[0]
    assert item["lane"] == "suggestion"
    assert item["kind"] == "graph_candidate"
    assert {a["id"] for a in item["actions"]} == {"approve", "reject"}
    assert item["graph_kinds"] == ["organization"]


def test_approve_corpus_org_creates_node(conn) -> None:
    cid = _make_candidate(conn, "Acme Corp", "organization")
    out = apply_graph_candidate_resolution(
        conn, cid, status="approved", payload={"relationship": "My company"}
    )
    assert out["ok"] is True
    node_id = out["node_id"]
    assert node_id
    row = conn.execute(
        "SELECT node_kind, title, confidence FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    assert row["node_kind"] == "organization"
    assert row["title"] == "Acme Corp"
    assert float(row["confidence"]) >= 0.8  # confirmed-by-user floor
    # candidate is resolved and no longer open
    assert not graph_candidate_list(conn, status="open")


def test_approve_corpus_person_links_to_me(conn) -> None:
    cid = _make_candidate(conn, "Jordan Lee", "person")
    out = apply_graph_candidate_resolution(
        conn, cid, status="approved", payload={"relationship": "college friend"}
    )
    assert out["ok"] is True
    row = conn.execute(
        "SELECT node_kind FROM graph_nodes WHERE node_id=?", (out["node_id"],)
    ).fetchone()
    assert row["node_kind"] == "person"


def test_reject_creates_no_node(conn) -> None:
    cid = _make_candidate(conn, "Random Inc", "organization")
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM graph_nodes WHERE status NOT IN ('scaffold','stub')"
    ).fetchone()["n"]
    out = apply_graph_candidate_resolution(conn, cid, status="rejected")
    assert out["ok"] is True
    after = conn.execute(
        "SELECT COUNT(*) AS n FROM graph_nodes WHERE status NOT IN ('scaffold','stub')"
    ).fetchone()["n"]
    assert after == before
