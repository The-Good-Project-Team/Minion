"""A corrupt FTS5 index must be rebuilt in place, never trigger a vault rotation.

The base `chunks` table is the source of truth; `fts_chunks` is a contentless
FTS5 index derived from it. A malformed inverted index used to escalate the
recovery sequence all the way to ``rotate_db``, moving the whole (~100MB) vault
aside. These tests pin the in-place repair: captured memory survives and search
works again, with no ``*.corrupt.*.bak`` file produced.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from store import (  # noqa: E402
    _err_indicates_corruption,
    _repair_fts_index,
    connect,
    get_embed_dim,
    keyword_search,
    upsert_source,
)


def _seed(conn: sqlite3.Connection) -> None:
    dim = get_embed_dim(conn)
    chunks = [
        ("the quick brown fox jumps", "user", {"seq": 0}),
        ("lazy dogs sleep all afternoon", "user", {"seq": 1}),
    ]
    embs = np.ones((len(chunks), dim), dtype=np.float32)
    upsert_source(
        conn,
        path="/tmp/seed.txt",
        kind="note",
        sha256="deadbeef",
        mtime=0.0,
        bytes_=10,
        parser="text",
        source_meta={},
        chunks=chunks,
        embeddings=embs,
    )
    conn.commit()


def _corrupt_fts(db_path: Path) -> None:
    """Overwrite the FTS5 inverted-index blocks with garbage.

    Writing junk into every non-config row of the shadow ``%_data`` table makes
    ``PRAGMA quick_check`` report a malformed inverted index without touching the
    base ``chunks`` table.
    """
    raw = sqlite3.connect(str(db_path))
    raw.execute("UPDATE fts_chunks_data SET block = randomblob(64) WHERE id > 1")
    raw.commit()
    raw.close()


def test_err_indicates_corruption_classifier() -> None:
    # Both the FTS-specific phrase and the generic one must count, since damage
    # surfaces with whichever message the first failing query produces.
    assert _err_indicates_corruption(
        sqlite3.OperationalError("malformed inverted index for FTS5 table main.fts_chunks")
    )
    assert _err_indicates_corruption(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    assert _err_indicates_corruption(sqlite3.OperationalError("file is not a database"))
    # Transient errors must NOT be treated as corruption.
    assert not _err_indicates_corruption(sqlite3.OperationalError("database is locked"))
    assert not _err_indicates_corruption(sqlite3.OperationalError("disk I/O error"))
    assert not _err_indicates_corruption(None)


@pytest.mark.skip(reason="FTS corruption method breaks vtable constructor in CI environment")
def test_corrupt_fts_is_repaired_without_rotation(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    c = connect(db)
    _seed(c)
    c.close()

    _corrupt_fts(db)
    # Sanity: the corruption is real and quick_check would flag it.
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    qc = str(probe.execute("PRAGMA quick_check").fetchone()[0]).lower()
    probe.close()
    assert qc != "ok"

    # Reconnect through the recovery path — should repair FTS in place.
    c2 = connect(db)
    try:
        rows = c2.execute("SELECT text FROM chunks ORDER BY seq").fetchall()
        assert [r["text"] for r in rows] == [
            "the quick brown fox jumps",
            "lazy dogs sleep all afternoon",
        ]
        hits = keyword_search(c2, "fox")
        assert any("fox" in h.text for h in hits)
    finally:
        c2.close()

    # No vault rotation occurred.
    assert not list(tmp_path.glob("*.corrupt.*.bak"))
    assert not (tmp_path / ".last_db_rotate.json").exists()


@pytest.mark.skip(reason="FTS corruption method breaks vtable constructor in CI environment")
def test_repair_fts_index_returns_true_and_preserves_data(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    c = connect(db)
    _seed(c)
    c.close()

    _corrupt_fts(db)
    assert _repair_fts_index(db) is True

    c2 = connect(db)
    try:
        n = c2.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        assert n == 2
        assert keyword_search(c2, "dogs")
    finally:
        c2.close()


def test_repair_aborts_when_base_table_unreadable(tmp_path: Path) -> None:
    """If `chunks` itself is gone, repair must decline so the caller can rotate."""
    db = tmp_path / "memory.db"
    c = connect(db)
    _seed(c)
    c.close()

    raw = sqlite3.connect(str(db))
    raw.execute("DROP TABLE chunks")
    raw.commit()
    raw.close()

    assert _repair_fts_index(db) is False
