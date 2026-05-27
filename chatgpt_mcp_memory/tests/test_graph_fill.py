"""Graph-fill queue: gaps 42 asks about."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from forty_two import next_question, reply
from graph_fill import _is_plausible_graph_title, apply_answer, open_thread_for_gap, pick_next_gap
from store import _new_id, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def _sparse_person(conn, name: str = "Alex") -> str:
    nid = _new_id("gn")
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0.5, '[]', 'vault_local', ?, ?)",
        (nid, name, now, now),
    )
    conn.commit()
    return nid


def test_pick_next_gap_sparse_person(conn) -> None:
    _sparse_person(conn, "Alex")
    gap = pick_next_gap(conn, None)
    assert gap is not None
    assert gap["gap_type"] == "person"
    assert gap["label"] == "Alex"


def test_sparse_person_reply_updates_summary(conn) -> None:
    nid = _sparse_person(conn, "Jordan")
    gap = pick_next_gap(conn, None)
    assert gap is not None
    out = open_thread_for_gap(conn, gap)
    tid = out["thread"]["thread_id"]
    result = apply_answer(
        conn,
        out["thread"],
        body="College friend from Portland.",
    )
    assert result["ok"] is True
    row = conn.execute("SELECT summary FROM graph_nodes WHERE node_id=?", (nid,)).fetchone()
    meta = json.loads(row["summary"] or "{}")
    assert "Portland" in str(meta.get("user_note") or "")
    assert result.get("follow_up")
    assert result.get("resolved") is False


def test_forty_two_reply_chain(conn) -> None:
    _sparse_person(conn, "Sam")
    nxt = next_question(conn, None)
    assert nxt["created"] is True
    tid = nxt["thread"]["thread_id"]
    out = reply(conn, tid, body="Neighbor who dog-sits.")
    assert out["ok"] is True
    thread = out["thread"]
    assert any("Sam" in str(m.get("body_md") or "") for m in thread["messages"])


def test_empty_bucket_reply_creates_node(conn) -> None:
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    gap = pick_next_gap(conn, None)
    assert gap is not None
    assert gap["gap_type"] == "bucket"
    assert gap["parent_node_id"] == "scaffold-people-friends"
    out = open_thread_for_gap(conn, gap)
    result = apply_answer(conn, out["thread"], body="Riley Chen")
    assert result["ok"] is True
    row = conn.execute(
        "SELECT title FROM graph_nodes WHERE parent_node_id='scaffold-people-friends' "
        "AND status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    titles = [str(r["title"]) for r in row]
    assert any("Riley" in t for t in titles)


def test_next_question_uses_graph_gap(conn) -> None:
    _sparse_person(conn, "Casey")
    out = next_question(conn, None)
    assert out["created"] is True
    body = out["thread"]["messages"][0]["body_md"]
    assert "Casey" in body
    # Template openings use "**42:**"; Gemini openings omit that prefix by design.
    assert "42" in body.lower() or "casey" in body.lower()


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
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM graph_nodes WHERE parent_node_id='scaffold-people-friends' "
        "AND status NOT IN ('scaffold', 'stub')"
    ).fetchone()
    assert int(row["c"]) == 0


def test_person_title_filter_rejects_tool_and_markdown_labels() -> None:
    assert _is_plausible_graph_title("A. Kim", node_kind="person")
    assert not _is_plausible_graph_title("`reiftauati", node_kind="person")
    assert not _is_plausible_graph_title("Openai Codex:reiftauati", node_kind="person")
