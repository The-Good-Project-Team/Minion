"""SQLite recovery must not rotate a healthy DB on transient errors."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from store import _db_looks_corrupt, connect, seed_sync_sources  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c, db
    c.close()


def test_db_looks_corrupt_ok_on_healthy_db(conn) -> None:
    c, db = conn
    assert _db_looks_corrupt(db, sqlite3.OperationalError("locked")) is False


def test_db_looks_corrupt_true_on_malformed_message(conn) -> None:
    c, db = conn
    assert _db_looks_corrupt(db, sqlite3.OperationalError("database disk image is malformed")) is True


def test_recent_db_rotation_flag(tmp_path) -> None:
    import json as _json
    import time as _time

    from store import recent_db_rotation

    data_dir = tmp_path
    flag = data_dir / ".last_db_rotate.json"

    # No flag -> not recent.
    assert recent_db_rotation(data_dir) is False

    # Fresh rotation -> recent.
    flag.write_text(_json.dumps({"rotated_at": _time.time()}), encoding="utf-8")
    assert recent_db_rotation(data_dir) is True

    # Old rotation -> not recent.
    flag.write_text(_json.dumps({"rotated_at": _time.time() - 100000}), encoding="utf-8")
    assert recent_db_rotation(data_dir, within_sec=900) is False

    # Garbage flag -> safe False, never raises.
    flag.write_text("not json", encoding="utf-8")
    assert recent_db_rotation(data_dir) is False
