"""Activity ask chat (corpus-backed)."""
from __future__ import annotations

from pathlib import Path

import pytest

from activity_chat import ask, list_ask_threads
from chat_store import _migrate_chat_schema
from store import connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    _migrate_chat_schema(c)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_ask_creates_thread_without_llm(conn) -> None:
    import os

    os.environ["MINION_ACTIVITY_CHAT_OFF"] = "1"
    try:
        out = ask(conn, message="What projects am I working on?")
        assert out["ok"] is True
        thread = out["thread"]
        assert thread is not None
        assert (thread.get("meta") or {}).get("mode") == "ask"
        msgs = thread.get("messages") or []
        assert len(msgs) >= 2
        assert msgs[0]["role"] == "user"
        assert msgs[-1]["role"] == "assistant"
        listed = list_ask_threads(conn)
        assert any(t["thread_id"] == thread["thread_id"] for t in listed["threads"])
    finally:
        os.environ.pop("MINION_ACTIVITY_CHAT_OFF", None)
