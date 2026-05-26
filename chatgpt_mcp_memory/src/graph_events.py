"""Append-only log of graph writes for the activity stream."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOG = "graph_events.jsonl"


def _log_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / _LOG


def log_graph_event(
    data_dir: Optional[Path],
    message: str,
    *,
    node_id: str = "",
    node_kind: str = "",
    action: str = "update",
) -> None:
    if not data_dir or not (message or "").strip():
        return
    path = _log_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(),
        "message": message.strip(),
        "node_id": node_id,
        "node_kind": node_kind,
        "action": action,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def graph_event_feed_items(
    data_dir: Optional[Path],
    *,
    since_ts: float,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    if not data_dir:
        return []
    path = _log_path(Path(data_dir))
    if not path.is_file():
        return []
    from activity_feed import _feed_item

    items: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines[-500:]):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = float(row.get("ts") or 0)
        if ts < since_ts:
            continue
        msg = str(row.get("message") or "").strip()
        if not msg:
            continue
        kind = str(row.get("node_kind") or "person")
        items.append(
            _feed_item(
                feed_id=f"graph-ev-{ts}-{row.get('node_id', '')}",
                ts=ts,
                lane="observed",
                kind="graph_update",
                title="Saved to graph",
                body=msg,
                graph_kinds=[kind] if kind else ["person"],
                refs={"node_id": str(row.get("node_id") or "")},
            )
        )
        if len(items) >= limit:
            break
    return items
