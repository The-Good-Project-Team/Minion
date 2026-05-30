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
from corpus_extract import _window  # noqa: E402
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


def test_window_overlap_and_count() -> None:
    pool = "".join(chr(65 + (i % 26)) for i in range(10000))  # 10k chars
    win = 2000
    slices = _window(pool, rounds=5, chars_per_round=win, overlap=0.25)
    # step = 1500; starts 0,1500,3000,4500,6000 -> 5 windows
    assert len(slices) == 5
    assert all(len(s) <= win for s in slices)
    # consecutive windows overlap by exactly 25% of the window (500 chars).
    assert slices[0][1500:2000] == slices[1][0:500]
    assert slices[1][1500:2000] == slices[2][0:500]


def test_window_small_pool_single_slice() -> None:
    assert _window("short text", rounds=5, chars_per_round=5000) == ["short text"]
    assert _window("", rounds=5, chars_per_round=5000) == []


def test_window_caps_at_rounds() -> None:
    pool = "x" * 100000
    assert len(_window(pool, rounds=3, chars_per_round=1000, overlap=0.25)) == 3


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
