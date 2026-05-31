"""Automatic memory lifecycle maintenance."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

import memory_lifecycle as ml
from memory_lifecycle import run_auto_maintenance
from store import (
    ambient_event_insert_ignore,
    connect,
    graph_candidate_create,
    seed_sync_sources,
    upsert_source,
)

DAY = 86400.0


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "memory.db")
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_auto_maintenance_purges_old_ambient(conn) -> None:
    old = time.time() - 30 * 86400
    ambient_event_insert_ignore(
        conn,
        event_type="window_focus",
        captured_at=old,
        dedupe_key="wf:old",
        payload={"app_name": "Test"},
    )
    conn.commit()
    out = run_auto_maintenance(conn, Path("/tmp"), force=True)
    assert out.get("skipped") is False
    assert out.get("ambient_events_purged", 0) >= 1


def _src_count(conn, kind: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM sources WHERE kind=?", (kind,)).fetchone()[0]


def test_trims_noise_keeps_user_content(conn, tmp_path: Path) -> None:
    """Forgetting policy: trim derived/noise, NEVER the user's own content."""
    now = time.time()

    # PROTECTED: a user document (kind=text), very old — must SURVIVE.
    upsert_source(
        conn, path="/docs/journal.md", kind="text", sha256="a", mtime=now - 400 * DAY,
        bytes_=10, parser="text", source_meta={},
        chunks=[("my private reflections", None, {})],
        embeddings=np.ones((1, 384), dtype=np.float32),
    )
    # DERIVED: an old ambient-summary source — must be PRUNED (>90d).
    upsert_source(
        conn, path="ambient/summary/old.md", kind="ambient-summary", sha256="b",
        mtime=now - 100 * DAY, bytes_=10, parser="ambient", source_meta={},
        chunks=[("app activity rollup", "summary", {})],
        embeddings=np.ones((1, 384), dtype=np.float32),
    )
    conn.execute(
        "INSERT INTO screen_memory_events(event_id, occurred_at, app, created_at, updated_at) "
        "VALUES('old', ?, 'X', ?, ?)", (now - 40 * DAY, now - 40 * DAY, now - 40 * DAY),
    )
    conn.execute(
        "INSERT INTO screen_memory_events(event_id, occurred_at, app, created_at, updated_at) "
        "VALUES('recent', ?, 'X', ?, ?)", (now, now, now),
    )
    res_id = graph_candidate_create(conn, candidate_type="corpus_entity", title="Done")
    conn.execute(
        "UPDATE graph_candidates SET status='approved', resolved_at=?, updated_at=? WHERE candidate_id=?",
        (now - 20 * DAY, now - 20 * DAY, res_id),
    )
    graph_candidate_create(conn, candidate_type="corpus_entity", title="Open")
    conn.execute(
        "INSERT INTO identity_claims(claim_id, kind, text, status, confidence, created_at, updated_at) "
        "VALUES('sup', 'fact', 'old name', 'superseded', 0.5, ?, ?)", (now - 40 * DAY, now - 40 * DAY),
    )
    conn.execute(
        "INSERT INTO identity_claims(claim_id, kind, text, status, confidence, created_at, updated_at) "
        "VALUES('act', 'fact', 'current name', 'active', 0.9, ?, ?)", (now, now),
    )
    conn.commit()

    out = run_auto_maintenance(conn, tmp_path, force=True)

    # PROTECTED untouched:
    assert _src_count(conn, "text") == 1, "user document must never be auto-deleted"
    assert conn.execute("SELECT COUNT(*) FROM identity_claims WHERE status='active'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM graph_candidates WHERE status='open'").fetchone()[0] == 1
    # NOISE trimmed:
    assert _src_count(conn, "ambient-summary") == 0
    assert conn.execute("SELECT COUNT(*) FROM screen_memory_events").fetchone()[0] == 1
    assert conn.execute("SELECT event_id FROM screen_memory_events").fetchone()[0] == "recent"
    assert conn.execute("SELECT COUNT(*) FROM graph_candidates WHERE status='approved'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM identity_claims WHERE status='superseded'").fetchone()[0] == 0
    assert out["screen_events_trimmed"] == 1 and out["candidates_trimmed"] == 1
    assert out["claims_trimmed"] == 1 and out["ambient_summaries_pruned"] == 1


def test_lifecycle_status_shape(conn) -> None:
    st = ml.lifecycle_status(conn)
    assert "counts" in st and "retention_days" in st and "protected_kinds" in st
    assert "text" in st["protected_kinds"]
