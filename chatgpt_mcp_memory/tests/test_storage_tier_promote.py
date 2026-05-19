"""Tests for chunk storage_tier promotion helpers."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from store import (  # noqa: E402
    connect,
    count_chunks_hot_to_warm_candidates,
    count_chunks_stale_source_tier_promotion_candidates,
    promote_chunks_for_stale_sources,
    promote_chunks_hot_to_warm_for_stale_sources,
    source_id_for,
    upsert_source,
)


def test_count_and_promote_stale_hot_to_warm(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    conn = connect(db)
    emb = np.zeros((1, 384), dtype=np.float32)
    upsert_source(
        conn,
        path="/stale-note.md",
        kind="text",
        sha256="a" * 64,
        mtime=1.0,
        bytes_=1,
        parser="text",
        source_meta={},
        chunks=[("hello", None, {})],
        embeddings=emb,
    )
    sid = source_id_for("/stale-note.md")
    old_ts = time.time() - 400 * 86400
    conn.execute("UPDATE sources SET updated_at = ? WHERE source_id = ?", (old_ts, sid))
    conn.commit()

    thr = time.time() - 365 * 86400
    assert count_chunks_hot_to_warm_candidates(conn, source_updated_before=thr) >= 1

    row = conn.execute(
        "SELECT storage_tier FROM chunks WHERE source_id = ?", (sid,)
    ).fetchone()
    assert row is not None
    assert row["storage_tier"] == "hot"

    delta = promote_chunks_hot_to_warm_for_stale_sources(conn, source_updated_before=thr)
    conn.commit()
    assert delta >= 1

    row2 = conn.execute(
        "SELECT storage_tier FROM chunks WHERE source_id = ?", (sid,)
    ).fetchone()
    assert row2["storage_tier"] == "warm"


def test_count_and_promote_stale_warm_to_cold(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    conn = connect(db)
    emb = np.zeros((1, 384), dtype=np.float32)
    upsert_source(
        conn,
        path="/stale-cold.md",
        kind="text",
        sha256="c" * 64,
        mtime=1.0,
        bytes_=1,
        parser="text",
        source_meta={},
        chunks=[("hello", None, {})],
        embeddings=emb,
    )
    sid = source_id_for("/stale-cold.md")
    old_ts = time.time() - 400 * 86400
    conn.execute("UPDATE sources SET updated_at = ? WHERE source_id = ?", (old_ts, sid))
    conn.commit()

    thr_warm = time.time() - 120 * 86400
    promote_chunks_hot_to_warm_for_stale_sources(conn, source_updated_before=thr_warm)
    conn.commit()

    row = conn.execute(
        "SELECT storage_tier FROM chunks WHERE source_id = ?", (sid,)
    ).fetchone()
    assert row is not None and row["storage_tier"] == "warm"

    thr_cold = time.time() - 365 * 86400
    assert (
        count_chunks_stale_source_tier_promotion_candidates(
            conn, source_updated_before=thr_cold, from_tier="warm"
        )
        >= 1
    )

    n = promote_chunks_for_stale_sources(
        conn,
        source_updated_before=thr_cold,
        from_tier="warm",
        to_tier="cold",
    )
    conn.commit()
    assert n >= 1

    row2 = conn.execute(
        "SELECT storage_tier FROM chunks WHERE source_id = ?", (sid,)
    ).fetchone()
    assert row2["storage_tier"] == "cold"


def test_kind_filter_excludes_non_matching_sources(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    conn = connect(db)
    emb = np.zeros((1, 384), dtype=np.float32)
    upsert_source(
        conn,
        path="/a.md",
        kind="text",
        sha256="b" * 64,
        mtime=1.0,
        bytes_=1,
        parser="text",
        source_meta={},
        chunks=[("x", None, {})],
        embeddings=emb,
    )
    sid = source_id_for("/a.md")
    old_ts = time.time() - 400 * 86400
    conn.execute("UPDATE sources SET updated_at = ? WHERE source_id = ?", (old_ts, sid))
    conn.commit()
    thr = time.time() - 365 * 86400
    n = count_chunks_hot_to_warm_candidates(conn, source_updated_before=thr, source_kinds=["pdf"])
    assert n == 0
