"""Regression guard: transient SQLite errors must NEVER rotate the vault aside.

A rotate-on-lock bug in Minion 1 wiped a user's index when a long writer held
the DB lock. 3.1.x gates the rotate rung behind corruption detection; this test
pins that invariant so it can't silently regress.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from store import _db_looks_corrupt, _err_indicates_corruption

# Transient / operational — the DB is fine, just unavailable this instant.
TRANSIENT = [
    "database is locked",
    "database is busy",
    "disk I/O error",
    "database or disk is full",
    "no space left on device",
    "unable to open database file",
]

# On-disk damage — rotating aside is the correct last resort.
CORRUPTION = [
    "database disk image is malformed",
    "file is not a database",
    "malformed database schema",
    "database corruption at line 1",
]


@pytest.fixture(autouse=True)
def _no_force_rotate(monkeypatch):
    monkeypatch.delenv("MINION_SQLITE_ROTATE_ON_FAILURE", raising=False)


@pytest.mark.parametrize("msg", TRANSIENT)
def test_transient_errors_never_rotate(msg: str) -> None:
    err = sqlite3.OperationalError(msg)
    assert _err_indicates_corruption(err) is False
    # Even with a missing path, a transient error must not be judged corrupt.
    assert _db_looks_corrupt(Path("/nonexistent/vault.db"), err) is False


@pytest.mark.parametrize("msg", CORRUPTION)
def test_corruption_signals_do_rotate(msg: str) -> None:
    err = sqlite3.DatabaseError(msg)
    assert _err_indicates_corruption(err) is True
    assert _db_looks_corrupt(Path("/nonexistent/vault.db"), err) is True
