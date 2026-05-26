"""Graph event log for activity stream."""
from __future__ import annotations

import json
from pathlib import Path

from graph_events import graph_event_feed_items, log_graph_event


def test_graph_event_feed_items(tmp_path: Path) -> None:
    log_graph_event(tmp_path, "Added Sam under Friends")
    items = graph_event_feed_items(tmp_path, since_ts=0, limit=5)
    assert len(items) == 1
    assert items[0]["kind"] == "graph_update"
    assert "Sam" in items[0]["body"]
