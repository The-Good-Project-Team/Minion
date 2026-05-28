"""Graph clarification chat threads."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from graph_clarify import next_clarification, reply
from store import _new_id, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_next_clarification_sparse_person(conn) -> None:
    nid = _new_id("gn")
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0.5, '[]', 'vault_local', ?, ?)",
        (nid, "Alex", now, now),
    )
    conn.commit()
    out = next_clarification(conn, None)
    assert out["created"] is True
    thread = out["thread"]
    assert thread is not None
    body = thread["messages"][0]["body_md"] or ""
    assert "Alex" in body
    assert (thread["messages"][0].get("meta") or {}).get("speaker") in ("42", "Minion")


def test_reply_resolves_person_summary(conn) -> None:
    nid = _new_id("gn")
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0.5, '[]', 'vault_local', ?, ?)",
        (nid, "Jordan", now, now),
    )
    conn.commit()
    out = next_clarification(conn, None)
    tid = out["thread"]["thread_id"]
    out = reply(conn, tid, body="College friend from Portland.")
    assert out["ok"] is True
    row = conn.execute("SELECT summary FROM graph_nodes WHERE node_id=?", (nid,)).fetchone()
    assert row is not None
    meta = json.loads(row["summary"] or "{}")
    assert "Portland" in str(meta.get("user_note") or "")
    thread = out["thread"]
    assert thread is not None
