"""Chat SSE helpers and 42 stream_reply."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from chat_sse import iter_text_deltas, sse_line
from librarian import next_question, stream_reply
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


def test_sse_line_roundtrip() -> None:
    line = sse_line("message.assistant.delta", {"delta": "hi"})
    assert line.startswith("event: message.assistant.delta\n")
    data_line = [ln for ln in line.split("\n") if ln.startswith("data:")][0]
    payload = json.loads(data_line[5:].strip())
    assert payload["delta"] == "hi"


def test_iter_text_deltas_chunks() -> None:
    parts = list(iter_text_deltas("abcdefghij", chunk_size=3))
    assert "".join(parts) == "abcdefghij"


def test_stream_reply_emits_sse_events(conn) -> None:
    _sparse_person(conn, "River")
    nxt = next_question(conn, None)
    tid = nxt["thread"]["thread_id"]
    events = list(stream_reply(conn, tid, body="Met at a hackathon in Austin."))
    kinds = [e["event"] for e in events]
    assert "message.user" in kinds
    assert "message.assistant.start" in kinds
    assert "message.assistant.delta" in kinds
    assert "message.assistant.done" in kinds
    done = next(e for e in events if e["event"] == "message.assistant.done")
    assert done["data"]["thread"]["thread_id"] == tid


def test_librarian_reply_stream_http(sidecar) -> None:
    """User objective: agent reply stream returns assistant SSE frames."""
    conn = connect(sidecar.data_dir / "memory.db")
    try:
        _sparse_person(conn, "Morgan")
        nxt = next_question(conn, None)
        tid = nxt["thread"]["thread_id"]
        conn.commit()
    finally:
        conn.close()

    r = sidecar.post(
        "/chat/agent/reply/stream",
        json_body={"message": "Coworker on the design team.", "thread_id": tid},
    )
    assert r.status_code == 200
    assert "text/event-stream" in (r.headers.get("content-type") or "")
    body = r.text
    assert "message.assistant.delta" in body
    assert "message.assistant.done" in body
    assert "event: done" in body
