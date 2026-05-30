"""Phantom person cleanup on the graph."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from chat_store import chat_thread_insert
from graph_fill import apply_answer, open_thread_for_gap, pick_next_gap
from graph_phantom import is_phantom_person_title, purge_phantom_graph_artifacts
from store import _new_id, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_is_phantom_person_title() -> None:
    assert is_phantom_person_title("hi there")
    assert is_phantom_person_title("hey")
    assert not is_phantom_person_title("Alex Chen")


def test_purge_removes_phantom_person_and_stale_thread(conn, tmp_path) -> None:
    nid = _new_id("gn")
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0.5, '[]', 'vault_local', ?, ?)",
        (nid, "hi there", now, now),
    )
    tid = chat_thread_insert(
        conn,
        subject_id=nid,
        topic="Librarian · graph",
        meta={"mode": "librarian", "gap": {"gap_type": "person_relation", "subject_id": nid, "label": "hi there"}},
    )
    conn.commit()

    out = purge_phantom_graph_artifacts(conn, commit=True)
    assert nid in out["removed_nodes"]
    assert tid in out["resolved_threads"]
    row = conn.execute("SELECT status FROM chat_threads WHERE thread_id=?", (tid,)).fetchone()
    assert row["status"] == "resolved"
    assert not conn.execute("SELECT 1 FROM graph_nodes WHERE node_id=?", (nid,)).fetchone()


def test_purge_closes_thread_when_subject_missing(conn) -> None:
    ghost = _new_id("gn")
    tid = chat_thread_insert(
        conn,
        subject_id=ghost,
        topic="Librarian · graph",
        meta={"mode": "librarian", "gap": {"gap_type": "person_relation", "subject_id": ghost, "label": "hi there"}},
    )
    conn.commit()
    out = purge_phantom_graph_artifacts(conn, commit=True)
    assert tid in out["resolved_threads"]


def test_pick_next_gap_skips_phantom_person(conn) -> None:
    nid = _new_id("gn")
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0.5, '[]', 'vault_local', ?, ?)",
        (nid, "hi there", now, now),
    )
    conn.commit()
    gap = pick_next_gap(conn, None)
    assert gap is None or gap.get("label") != "hi there"


def test_bucket_rejects_greeting_as_name(conn) -> None:
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    gap = pick_next_gap(conn, None)
    assert gap is not None
    assert gap["gap_type"] == "bucket"
    out = open_thread_for_gap(conn, gap)
    result = apply_answer(conn, out["thread"], body="hi there")
    assert result["ok"] is True
    assert result.get("resolved") is False
    assert "doesn't look like a name" in " ".join(result.get("deltas") or [])
