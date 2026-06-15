"""Ingest progress counters must not expose stale done/total pairs to the UI."""
from __future__ import annotations

import api as minion_api


def _reset_active() -> None:
    with minion_api.State.active_lock:
        minion_api.State.active = {
            "root": None,
            "total": 0,
            "done": 0,
            "added": 0,
            "skipped": 0,
        }


def test_public_active_sanitizes_stale_counters() -> None:
    _reset_active()
    with minion_api.State.active_lock:
        minion_api.State.active = {
            "root": None,
            "total": 0,
            "done": 69,
            "added": 42,
            "skipped": 1,
        }
    assert minion_api._public_active() == {
        "root": None,
        "total": 0,
        "done": 0,
        "added": 0,
        "skipped": 0,
    }


def test_file_done_ignored_after_batch_done() -> None:
    _reset_active()
    bridge = minion_api._watcher_event_bridge
    minion_api.State.loop = None  # skip WS fanout in unit test

    bridge("batch_started", {"total": 2})
    bridge(
        "file_done",
        {"index": 1, "total": 2, "source_id": "a", "skipped": False},
    )
    bridge(
        "file_done",
        {"index": 2, "total": 2, "source_id": "b", "skipped": False},
    )
    bridge("batch_done", {"total": 2})
    # Late duplicate from the finished batch must not resurrect done/total.
    bridge(
        "file_done",
        {"index": 2, "total": 2, "source_id": "b", "skipped": False},
    )

    assert minion_api._public_active() == {
        "root": None,
        "total": 0,
        "done": 0,
        "added": 0,
        "skipped": 0,
    }
