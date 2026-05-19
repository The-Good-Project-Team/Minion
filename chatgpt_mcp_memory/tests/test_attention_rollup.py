"""Attention rollup from ambient_events."""
from __future__ import annotations

import json
import time

import pytest

from attention_rollup import rollup_attention
from store import ambient_event_insert_ignore, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "memory.db")
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_rollup_top_apps(conn) -> None:
    now = time.time()
    for i, app in enumerate(["Mail", "Mail", "Chrome"]):
        ambient_event_insert_ignore(
            conn,
            event_type="window_focus",
            captured_at=now + i * 10,
            dedupe_key=f"wf:{i}",
            payload={"app_name": app, "window_title": "t"},
        )
    conn.commit()
    r = rollup_attention(conn, since_ts=now - 1, limit=50)
    assert r["event_count"] >= 3
    assert r["top_apps"]
    assert "summary_line" in r
