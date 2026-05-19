"""Tests for activity feed and graph scaffold."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from activity_feed import build_activity_feed
from store import (
    ambient_event_insert_ignore,
    connect,
    graph_scaffold_list,
    seed_sync_sources,
    sync_job_run_append,
    task_infer_insert,
    wiki_page_upsert,
)


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_graph_scaffold_has_me_tree(conn) -> None:
    g = graph_scaffold_list(conn)
    assert g["root"] is not None
    assert g["root"]["title"] == "Me"
    assert len(g["root"]["children"]) >= 10
    assert "person" in g["node_types"]
    assert "knows" in g["relation_types"]


def test_graph_scaffold_counts_wiki_person(conn) -> None:
    wiki_page_upsert(
        conn,
        page_id=None,
        page_type="person",
        title="Alex",
        body_md="Friend",
    )
    conn.commit()
    g = graph_scaffold_list(conn)
    people = next(c for c in g["root"]["children"] if c["title"] == "People")
    assert people["filled_count"] >= 1


def test_activity_feed_merges_lanes(conn, tmp_path: Path) -> None:
    now = time.time()
    ambient_event_insert_ignore(
        conn,
        event_type="window_focus",
        captured_at=now,
        dedupe_key=f"wf:{now}:Slack\x1fstandup",
        payload={"app_name": "Slack", "window_title": "standup"},
    )
    sync_job_run_append(
        conn,
        source_key="ambient_loop",
        status="ok",
        started_at=now - 5,
        finished_at=now,
        items_count=2,
    )
    task_infer_insert(conn, title="Follow up standup", origin="inferred")
    wiki_page_upsert(
        conn,
        page_id=None,
        page_type="person",
        title="Teammate",
        body_md="From standup",
    )
    conn.commit()

    feed = build_activity_feed(conn, tmp_path, limit=50, since_hours=24)
    assert feed["now"] is None or feed["now"]["lane"] == "now"
    assert len(feed["items"]) >= 3
    lanes = {i["lane"] for i in feed["items"]}
    assert "observed" in lanes
    assert "parsed" in lanes
    assert "suggestion" in lanes
    assert feed["graph"]["root"]["title"] == "Me"
