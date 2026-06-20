"""Track original files that were temporarily staged for embedding."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict


TRACKING_JSONL = "file_tracking.jsonl"
STATE_JSON = "file_tracking_state.json"


def register_tracked_path(
    data_dir: Path,
    *,
    original_path: Path,
    staged_path: Path,
    kind: str,
) -> None:
    original = Path(original_path).expanduser().resolve()
    staged = Path(staged_path).expanduser().resolve()
    try:
        st = original.stat()
        exists = True
        size = st.st_size
        mtime = st.st_mtime
    except OSError:
        exists = False
        size = 0
        mtime = 0.0
    row = {
        "original_path": str(original),
        "staged_path": str(staged),
        "kind": kind,
        "exists": exists,
        "size": size,
        "mtime": mtime,
        "registered_at": time.time(),
    }
    path = Path(data_dir) / TRACKING_JSONL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def start_file_tracker(data_dir: Path, *, interval_sec: float = 60.0) -> threading.Thread:
    data = Path(data_dir).expanduser().resolve()

    def _run() -> None:
        while True:
            try:
                scan_tracked_files(data)
            except Exception:
                pass
            time.sleep(max(5.0, float(interval_sec)))

    t = threading.Thread(target=_run, name="minion-file-tracker", daemon=True)
    t.start()
    return t


def scan_tracked_files(data_dir: Path) -> Dict[str, Any]:
    tracked = _latest_tracked_rows(data_dir)
    state_path = Path(data_dir) / STATE_JSON
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    events = []
    for original, row in tracked.items():
        prev = state.get(original) if isinstance(state.get(original), dict) else row
        current = _current_snapshot(original)
        event_type = ""
        if bool(prev.get("exists", True)) and not current["exists"]:
            event_type = "tracked_file_missing"
        elif current["exists"] and (
            int(prev.get("size", 0)) != int(current["size"])
            or float(prev.get("mtime", 0.0)) != float(current["mtime"])
        ):
            event_type = "tracked_file_modified"
        state[original] = {**row, **current, "checked_at": time.time()}
        if event_type:
            event = {
                "ts": time.time(),
                "kind": event_type,
                "app_name": "Minion",
                "window_title": "Tracked source file changed",
                "summary": f"{event_type}: {original}",
                "original_path": original,
                "staged_path": row.get("staged_path"),
                "dedupe_key": f"{event_type}:{original}:{current.get('mtime')}:{current.get('size')}",
            }
            _append_ambient_event(data_dir, event)
            events.append(event)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"tracked": len(tracked), "events": len(events)}


def _latest_tracked_rows(data_dir: Path) -> Dict[str, Dict[str, Any]]:
    path = Path(data_dir) / TRACKING_JSONL
    rows: Dict[str, Dict[str, Any]] = {}
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        original = str(row.get("original_path") or "").strip()
        if original:
            rows[original] = row
    return rows


def _current_snapshot(path: str) -> Dict[str, Any]:
    try:
        st = Path(path).stat()
        return {"exists": True, "size": st.st_size, "mtime": st.st_mtime}
    except OSError:
        return {"exists": False, "size": 0, "mtime": 0.0}


def _append_ambient_event(data_dir: Path, event: Dict[str, Any]) -> None:
    path = Path(data_dir) / "ambient" / "stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def find_original_path(data_dir: Path, staged_path: Path) -> Optional[str]:
    """Look up the original path for a given staged path from file_tracking.jsonl.
    
    Used to resolve reveal paths when temporary ingest deletes the inbox copy.
    Returns None if no matching entry is found.
    """
    staged = Path(staged_path).expanduser().resolve()
    path = Path(data_dir) / TRACKING_JSONL
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        row_staged = row.get("staged_path")
        if row_staged and Path(row_staged).expanduser().resolve() == staged:
            original = row.get("original_path")
            if original:
                return str(original)
    return None
