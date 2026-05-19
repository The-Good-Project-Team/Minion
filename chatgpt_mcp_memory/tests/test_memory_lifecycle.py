"""Automatic memory lifecycle maintenance."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from memory_lifecycle import run_auto_maintenance
from store import ambient_event_insert_ignore, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "memory.db")
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_auto_maintenance_purges_old_ambient(conn) -> None:
    old = time.time() - 30 * 86400
    ambient_event_insert_ignore(
        conn,
        event_type="window_focus",
        captured_at=old,
        dedupe_key="wf:old",
        payload={"app_name": "Test"},
    )
    conn.commit()
    out = run_auto_maintenance(conn, Path("/tmp"), force=True)
    assert out.get("skipped") is False
    assert out.get("ambient_events_purged", 0) >= 1
