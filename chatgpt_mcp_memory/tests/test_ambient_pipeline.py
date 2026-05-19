"""Multi-kind ambient JSONL ingest."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ambient_pipeline import ingest_ambient_jsonl
from store import ambient_events_since, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c, tmp_path
    c.close()


def _write_stream(data_dir: Path, records: list) -> None:
    p = data_dir / "ambient" / "stream.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ingest_multiple_kinds(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {"ts": now, "kind": "window_focus", "app_name": "Mail", "window_title": "Inbox"},
            {
                "ts": now + 1,
                "kind": "browser_visit",
                "app_name": "Safari",
                "url_or_host": "example.com",
            },
            {
                "ts": now + 2,
                "kind": "fs_event",
                "path": "/Users/me/Projects/foo.rs",
                "path_display": "foo.rs",
                "op": "modify",
            },
            {"ts": now + 3, "kind": "process_snapshot", "apps": [{"name": "Mail"}], "dedupe_key": "proc:1"},
        ],
    )
    out = ingest_ambient_jsonl(data_dir=data_dir, conn=c, max_lines=50)
    c.commit()
    assert out["ingested"] >= 2
    types = {e["event_type"] for e in ambient_events_since(c, since_ts=0, limit=20)}
    assert "window_focus" in types
    assert "browser_visit" in types
    assert "fs_event" not in types


def test_window_snapshot_ingest(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "window_snapshot",
                "app_name": "Slack",
                "window_title": "general",
                "window_id": "42",
                "ax_hash": "abc123",
                "ax_text_sample": "hello from slack",
                "dedupe_key": "ws:42:abc123",
            },
        ],
    )
    out = ingest_ambient_jsonl(data_dir=data_dir, conn=c, max_lines=50)
    c.commit()
    assert out["ingested"] == 1
    events = ambient_events_since(c, since_ts=0, limit=10)
    assert events[0]["event_type"] == "window_snapshot"
    assert events[0]["payload"]["app_name"] == "Slack"


def test_listening_wake_ingest(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "listening_wake",
                "session_id": "fl_1",
                "transcript_excerpt": "hey minion what's up",
                "dedupe_key": "listen_wake:fl_1:0001.wav",
            },
        ],
    )
    out = ingest_ambient_jsonl(data_dir=data_dir, conn=c, max_lines=50)
    c.commit()
    assert out["ingested"] == 1
    events = ambient_events_since(c, since_ts=0, limit=10)
    assert events[0]["event_type"] == "listening_wake"
    assert "minion" in events[0]["payload"]["transcript_excerpt"].lower()


def test_screenshot_fallback_dedupe_with_path(conn) -> None:
    c, data_dir = conn
    _write_stream(
        data_dir,
        [
            {
                "ts": time.time(),
                "kind": "screenshot_fallback",
                "screenshot_inbox_rel": "screen-memory/foo.png",
                "app_name": "Chrome",
            },
        ],
    )
    out = ingest_ambient_jsonl(data_dir=data_dir, conn=c, max_lines=10)
    c.commit()
    assert out["ingested"] == 1
    events = ambient_events_since(c, since_ts=0, limit=5)
    assert events[0]["event_type"] == "screenshot_fallback"
    assert events[0]["dedupe_key"] == "shot:screen-memory/foo.png"


def test_fs_secret_path_skipped(conn) -> None:
    c, data_dir = conn
    _write_stream(
        data_dir,
        [{"ts": time.time(), "kind": "fs_event", "path": "/Users/me/.env", "op": "modify"}],
    )
    out = ingest_ambient_jsonl(data_dir=data_dir, conn=c)
    c.commit()
    assert out["ingested"] == 0
