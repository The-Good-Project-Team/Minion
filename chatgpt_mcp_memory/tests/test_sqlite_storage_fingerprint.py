"""Tests for sqlite_storage_fingerprint (maintenance telemetry)."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from store import connect, sqlite_storage_fingerprint  # noqa: E402


def test_sqlite_storage_fingerprint_on_empty_db(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    conn = connect(db)
    fp = sqlite_storage_fingerprint(conn)
    assert fp["page_count"] >= 1
    assert fp["page_size"] >= 512
    assert fp["logical_bytes"] == fp["page_count"] * fp["page_size"]
    assert fp["freelist_ratio"] >= 0.0
    assert fp["freelist_ratio"] <= 1.0
    assert fp["db_path"]
    assert fp["db_file_bytes"] is not None and fp["db_file_bytes"] > 0
