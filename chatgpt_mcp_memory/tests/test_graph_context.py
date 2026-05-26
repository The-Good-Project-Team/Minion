"""Graph context and candidate queue."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from graph_context import build_graph_context, build_menu_status
from graph_fill import apply_graph_candidate_resolution
from store import (
    ambient_event_insert_ignore,
    connect,
    graph_candidate_create,
    graph_candidate_list,
    graph_candidate_resolve,
    seed_sync_sources,
)


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "memory.db")
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_graph_candidate_queue_round_trip(conn) -> None:
    cid = graph_candidate_create(
        conn,
        candidate_type="person_merge",
        title="Merge Alex?",
        payload={"existing_node_id": "gn1"},
        evidence_refs=["chunk:c1"],
        confidence=0.72,
        source="test",
    )
    conn.commit()
    rows = graph_candidate_list(conn)
    assert rows[0]["candidate_id"] == cid
    assert rows[0]["payload"]["existing_node_id"] == "gn1"

    resolved = graph_candidate_resolve(conn, cid, status="approved", payload_merge={"approved_by": "test"})
    assert resolved is not None
    assert resolved["status"] == "approved"
    assert resolved["payload"]["approved_by"] == "test"
    assert graph_candidate_list(conn) == []


def test_screen_entity_candidate_approval_writes_person_graph(conn, tmp_path: Path) -> None:
    cid = graph_candidate_create(
        conn,
        candidate_type="screen_entity",
        title="Who is Alex Kim?",
        payload={
            "entity_type": "email",
            "label": "Alex Kim",
            "email": "alex@example.com",
            "screen_event_id": "screen-1",
            "app": "Google Sheets",
            "window": "Investor leads",
        },
        evidence_refs=["screen_event:screen-1"],
        confidence=0.72,
        source="screen_memory",
    )
    conn.commit()

    out = apply_graph_candidate_resolution(
        conn,
        cid,
        status="approved",
        payload={"relationship": "investor lead"},
        data_dir=tmp_path,
    )
    conn.commit()

    assert out["ok"] is True
    assert out["node_id"]
    row = conn.execute(
        "SELECT title, summary FROM graph_nodes WHERE node_id=?", (out["node_id"],)
    ).fetchone()
    assert row["title"] == "Alex Kim"
    assert "alex@example.com" in row["summary"]
    assert "investor lead" in row["summary"]
    edge = conn.execute(
        "SELECT 1 FROM graph_edges WHERE from_node_id='scaffold-me' AND to_node_id=? "
        "AND rel_kind='knows'",
        (out["node_id"],),
    ).fetchone()
    assert edge is not None
    resolved = graph_candidate_list(conn, status="approved")
    assert resolved[0]["payload"]["resolved_node_id"] == out["node_id"]
    assert graph_candidate_list(conn) == []


def test_graph_context_exports_graph_candidates_and_focus_hints(conn, tmp_path: Path) -> None:
    now = time.time()
    ambient_event_insert_ignore(
        conn,
        event_type="window_focus",
        captured_at=now,
        dedupe_key=f"wf:{now}:Slack",
        payload={"app_name": "Slack", "window_title": "Alex project"},
    )
    graph_candidate_create(
        conn,
        candidate_type="person_merge",
        title="Is Alex the same person?",
        confidence=0.7,
    )
    conn.commit()

    ctx = build_graph_context(conn, tmp_path)
    assert ctx["graph"]["user_node_count"] >= 0
    assert ctx["open_candidates"]
    assert ctx["recent_ambient"][0]["app_name"] == "Slack"
    assert ctx["recent_ambient_hints"][0]["window_title"] == "Alex project"

    status = build_menu_status(conn, tmp_path)
    assert status["pending_questions"] >= 1
    assert status["open_candidates"] == 1
    assert status["should_notify"] is True
    assert status["next_question"]["kind"] == "graph_candidate"
    assert status["next_question"]["action"] == "resolve_graph_candidate"
    assert status["next_question"]["title"] == "Is Alex the same person?"
