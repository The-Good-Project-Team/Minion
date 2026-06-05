"""Minion 1 -> Minion 2 vault import: two-phase re-embed, adopt, idempotency."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import migrate_from_legacy as mig
import store


def _build_legacy(db_path: Path, dim: int, sources) -> int:
    """sources: list of (path, mtime, [texts]). Returns total chunk count."""
    conn = store.connect(db_path, embed_dim=dim)
    rng = np.random.RandomState(0)
    total = 0
    for spath, mtime, texts in sources:
        embs = rng.rand(len(texts), dim).astype("float32")
        store.upsert_source(
            conn,
            path=spath,
            kind="text",
            sha256="a" * 64,
            mtime=mtime,
            bytes_=100,
            parser="text",
            source_meta={},
            chunks=[(t, None, {"seq": i}) for i, t in enumerate(texts)],
            embeddings=embs,
        )
        total += len(texts)
    conn.commit()
    conn.close()
    return total


def _fts_hits(conn, term: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM fts_chunks WHERE fts_chunks MATCH ?", (term,)
    ).fetchone()
    return int(row["n"])


def _vec_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM vec_chunks").fetchone()["n"])


@pytest.fixture(autouse=True)
def _deterministic_embeddings(monkeypatch):
    # Phase B re-embeds with the 768-dim model; use the deterministic stand-in
    # so the test needs no model download / network.
    monkeypatch.setenv("MINION_DETERMINISTIC_EMBEDDINGS", "1")
    monkeypatch.setenv("MINION_TEST_EMBED_DIM", "768")


def test_phase_a_gives_instant_keyword_coverage(tmp_path: Path) -> None:
    """Text copy lands before any vectors: FTS works, vec table still empty."""
    legacy = tmp_path / "legacy" / "memory.db"
    legacy.parent.mkdir(parents=True)
    _build_legacy(legacy, 384, [("/n.md", 1.0, ["swipe scrolling habit reset"])])

    new_db = tmp_path / "new" / "memory.db"
    new_db.parent.mkdir(parents=True)
    conn = store.connect(new_db, embed_dim=768)
    ro = mig._open_legacy_ro(legacy)
    mig._phase_a_copy_text(conn, ro)
    ro.close()

    assert store.count_chunks(conn) == 1
    assert _vec_count(conn) == 0  # no vectors yet
    assert _fts_hits(conn, "scrolling") == 1  # keyword search already works
    assert store.get_meta(conn, "legacy_import") == "in_progress"


def test_full_import_reembeds_384_to_768(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy" / "memory.db"
    legacy.parent.mkdir(parents=True)
    n = _build_legacy(
        legacy,
        384,
        [
            ("/old.md", 1.0, ["older note about gardening"]),
            ("/new.md", 9.0, ["recent note about swiping less"]),
        ],
    )

    data_dir = tmp_path / "Minion 2" / "data"
    data_dir.mkdir(parents=True)
    events = []
    res = mig.run(
        data_dir, legacy_db=legacy, on_event=lambda k, p: events.append((k, p))
    )

    assert res["status"] == "imported"
    conn = store.connect(data_dir / "memory.db", embed_dim=768)
    assert store.get_embed_dim(conn) == 768
    assert store.count_chunks(conn) == n
    assert _vec_count(conn) == n  # every chunk re-embedded
    assert _fts_hits(conn, "swiping") == 1
    assert store.get_meta(conn, "legacy_import") == "done"
    # progress surfaced
    kinds = [k for k, _ in events]
    assert "batch_started" in kinds and "batch_done" in kinds


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy" / "memory.db"
    legacy.parent.mkdir(parents=True)
    _build_legacy(legacy, 384, [("/n.md", 1.0, ["alpha beta gamma"])])
    data_dir = tmp_path / "Minion 2" / "data"
    data_dir.mkdir(parents=True)

    first = mig.run(data_dir, legacy_db=legacy)
    assert first["status"] == "imported"
    second = mig.run(data_dir, legacy_db=legacy)
    assert second["status"] == "already-imported"

    conn = store.connect(data_dir / "memory.db", embed_dim=768)
    assert _vec_count(conn) == store.count_chunks(conn) == 1


def test_resume_refills_missing_vectors(tmp_path: Path) -> None:
    """Simulate a crash mid-Phase-B: marker in_progress, some vecs missing."""
    legacy = tmp_path / "legacy" / "memory.db"
    legacy.parent.mkdir(parents=True)
    _build_legacy(legacy, 384, [("/n.md", 1.0, ["one two", "three four", "five six"])])
    data_dir = tmp_path / "Minion 2" / "data"
    data_dir.mkdir(parents=True)

    # Phase A only, then delete vectors to mimic an interrupted Phase B.
    conn = store.connect(data_dir / "memory.db", embed_dim=768)
    ro = mig._open_legacy_ro(legacy)
    mig._phase_a_copy_text(conn, ro)
    ro.close()
    conn.execute("DELETE FROM vec_chunks")
    conn.commit()
    assert _vec_count(conn) == 0
    assert store.get_meta(conn, "legacy_import") == "in_progress"
    conn.close()

    res = mig.run(data_dir, legacy_db=legacy)
    assert res["status"] == "imported"
    conn = store.connect(data_dir / "memory.db", embed_dim=768)
    assert _vec_count(conn) == 3
    assert store.get_meta(conn, "legacy_import") == "done"


def test_adopt_same_dim_copies_vectors_verbatim(tmp_path: Path) -> None:
    """A 768-dim legacy vault is adopted wholesale — no re-embed."""
    legacy = tmp_path / "legacy" / "memory.db"
    legacy.parent.mkdir(parents=True)
    _build_legacy(legacy, 768, [("/n.md", 1.0, ["keep these exact vectors"])])
    # capture original vector
    lconn = store.connect(legacy, embed_dim=768)
    orig = lconn.execute("SELECT embedding FROM vec_chunks").fetchone()["embedding"]
    lconn.close()

    data_dir = tmp_path / "Minion 2" / "data"
    data_dir.mkdir(parents=True)
    res = mig.run(data_dir, legacy_db=legacy)
    assert res["status"] == "adopted"

    conn = store.connect(data_dir / "memory.db", embed_dim=768)
    got = conn.execute("SELECT embedding FROM vec_chunks").fetchone()["embedding"]
    assert bytes(got) == bytes(orig)  # copied, not recomputed
    assert store.get_meta(conn, "legacy_import") == "done"


def test_skip_when_vault_not_empty(tmp_path: Path) -> None:
    """Never auto-import into a vault that already holds content."""
    legacy = tmp_path / "legacy" / "memory.db"
    legacy.parent.mkdir(parents=True)
    _build_legacy(legacy, 384, [("/n.md", 1.0, ["legacy stuff"])])

    data_dir = tmp_path / "Minion 2" / "data"
    data_dir.mkdir(parents=True)
    # Pre-populate the new vault with unrelated content.
    _build_legacy(data_dir / "memory.db", 768, [("/mine.md", 1.0, ["my own note"])])

    res = mig.run(data_dir, legacy_db=legacy)
    assert res["status"] == "skip"
    conn = store.connect(data_dir / "memory.db", embed_dim=768)
    assert store.count_chunks(conn) == 1  # untouched


def test_skip_when_no_legacy(tmp_path: Path) -> None:
    data_dir = tmp_path / "Minion 2" / "data"
    data_dir.mkdir(parents=True)
    res = mig.run(data_dir, legacy_db=tmp_path / "nope" / "memory.db")
    assert res["status"] == "skip"
