"""Unit tests for SQLite journal mode selection in store.connect."""
from __future__ import annotations

import sqlite3
import sys
import os
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from store import _apply_journal_mode  # noqa: E402


def test_apply_journal_mode_delete_path_uses_real_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    try:
        mode = _apply_journal_mode(conn, db_path, wal_first=False)
    finally:
        conn.close()
    assert mode.lower() == "delete"


def test_apply_journal_mode_wal_success_uses_real_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    try:
        mode = _apply_journal_mode(conn, db_path)
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_apply_journal_mode_honors_forced_delete_with_real_sqlite(tmp_path: Path) -> None:
    old = os.environ.get("MINION_SQLITE_JOURNAL")
    os.environ["MINION_SQLITE_JOURNAL"] = "delete"
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    try:
        mode = _apply_journal_mode(conn, db_path)
    finally:
        conn.close()
        if old is None:
            os.environ.pop("MINION_SQLITE_JOURNAL", None)
        else:
            os.environ["MINION_SQLITE_JOURNAL"] = old
    assert mode.lower() == "delete"
