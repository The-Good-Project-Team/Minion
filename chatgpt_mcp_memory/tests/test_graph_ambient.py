"""Tests for ambient graph enrichment and graph spine."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from graph_ambient import build_graph_spine, enrich_graph_from_ambient
from store import ambient_event_insert_ignore, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_enrich_graph_from_clipboard_email(conn, tmp_path: Path) -> None:
    now = time.time()
    ambient_event_insert_ignore(
        conn,
        event_type="clipboard_event",
        captured_at=now,
        dedupe_key=f"clip:{now}",
        payload={
            "detected_emails": ["kate@practiceoflife.com"],
            "app_name": "Messages",
        },
    )
    conn.commit()
    out = enrich_graph_from_ambient(conn, tmp_path, since_hours=1)
    assert out["signals"] >= 1
    row = conn.execute(
        "SELECT node_id, title FROM graph_nodes WHERE node_kind='person' "
        "AND title LIKE 'Kate%'"
    ).fetchone()
    assert row is not None
    refs = conn.execute(
        "SELECT source_refs_json FROM graph_nodes WHERE node_id=?",
        (str(row["node_id"]),),
    ).fetchone()
    assert "amb:" in str(refs["source_refs_json"])


def test_enrich_graph_creates_project_after_repeated_title(conn, tmp_path: Path) -> None:
    now = time.time()
    title = "Practice of Life — Client Site"
    for i in range(2):
        ambient_event_insert_ignore(
            conn,
            event_type="window_focus",
            captured_at=now + i,
            dedupe_key=f"wf:{now + i}:{title}",
            payload={"app_name": "Google Chrome", "window_title": title},
        )
    conn.commit()
    out = enrich_graph_from_ambient(conn, tmp_path, since_hours=1)
    assert out["touched"] >= 1
    row = conn.execute(
        "SELECT node_id FROM graph_nodes WHERE node_kind='project' "
        "AND lower(title)=lower(?)",
        (title,),
    ).fetchone()
    assert row is not None


def test_enrich_graph_touches_existing_person(conn, tmp_path: Path) -> None:
    from entity_resolution import ensure_person_node, link_belongs_to_scaffold

    pid = ensure_person_node(conn, label="Alex Rivera", meta={"email": "alex@example.com"})
    link_belongs_to_scaffold(conn, pid)
    now = time.time()
    ambient_event_insert_ignore(
        conn,
        event_type="window_focus",
        captured_at=now,
        dedupe_key=f"wf:{now}:alex",
        payload={"app_name": "Slack", "window_title": "DM with Alex Rivera"},
    )
    conn.commit()
    out = enrich_graph_from_ambient(conn, tmp_path, since_hours=1)
    assert out["touched"] >= 1
    summary = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id=?", (pid,)
    ).fetchone()["summary"]
    meta = json.loads(summary)
    assert meta.get("recent_attention")


def test_build_graph_spine_lists_buckets(conn, tmp_path: Path) -> None:
    from entity_resolution import ensure_person_node, link_belongs_to_scaffold

    pid = ensure_person_node(conn, label="Jordan Lee")
    link_belongs_to_scaffold(conn, pid)
    conn.commit()
    spine = build_graph_spine(conn, tmp_path)
    assert spine.get("spine_md")
    assert spine.get("totals", {}).get("person", 0) >= 1
    assert any(b.get("filled_count", 0) > 0 for b in spine.get("buckets") or [])
