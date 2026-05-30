from __future__ import annotations

import json
from pathlib import Path

from file_tracker import register_tracked_path, scan_tracked_files


def test_file_tracker_emits_modified_event(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    original = tmp_path / "note.md"
    staged = data_dir / "inbox" / "note.md"
    original.write_text("first", encoding="utf-8")
    staged.parent.mkdir(parents=True)
    staged.write_text("first", encoding="utf-8")

    register_tracked_path(data_dir, original_path=original, staged_path=staged, kind="file")
    assert scan_tracked_files(data_dir)["events"] == 0

    original.write_text("second update", encoding="utf-8")
    out = scan_tracked_files(data_dir)

    assert out["events"] == 1
    stream = data_dir / "ambient" / "stream.jsonl"
    rows = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["kind"] == "tracked_file_modified"
    assert rows[-1]["original_path"] == str(original.resolve())
