"""One-shot, idempotent import of a Minion 1 vault into Minion 2.

Minion 1 (384-dim, all-MiniLM-L6-v2) and Minion 2 (768-dim, BAAI/bge-base-en-v1.5)
keep separate data dirs (see api.DATA_DIR_NAME / lib.rs). On Minion 2's first
launch this module imports Minion 1's content from the *old* shared "Minion" dir
so the transition is seamless. Minion 1's DB is opened read-only and never
mutated — it stays a safe fallback.

Because the two embedders have incompatible vector widths, Minion 1's vectors
cannot be reused; only the preserved chunk *text* migrates. The import is
two-phase so the app is usable immediately:

  Phase A  — copy every source + chunk (text + metadata) in one transaction.
             The chunks_ai_fts trigger populates FTS on insert, so keyword
             search over the whole corpus works the instant Minion 2 opens.
  Phase B  — re-embed the copied chunks with the 768-dim model, newest source
             first, inserting vec_chunks rows in batches. Resumable: only chunks
             still lacking a vector are processed, so a crash just resumes.

If the legacy DB happens to already be 768-dim (a Minion 2 vault that lived in
the old dir), it is adopted wholesale — sources, chunks and vectors copied as-is,
no re-embed.

Entry point: ``run(data_dir, on_event=...)``. Safe to call on every startup;
it no-ops once the import is marked done.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

import ingest
import store

log = logging.getLogger("minion.migrate")

EventFn = Callable[[str, dict], None]

_LEGACY_DIR_NAME = "Minion"          # Minion 1's Application Support folder
_STATE_KEY = "legacy_import"         # meta marker: "in_progress" | "done"
_EMBED_BATCH = 256                   # chunks re-embedded + committed per batch


def _noop(_kind: str, _payload: dict) -> None:
    pass


def _legacy_db_path(data_dir: Path) -> Path:
    # data_dir = .../Application Support/Minion 2/data
    #   .parent        -> .../Minion 2
    #   .parent.parent -> .../Application Support
    return data_dir.parent.parent / _LEGACY_DIR_NAME / "data" / store.DB_FILENAME


def _open_legacy_ro(path: Path) -> sqlite3.Connection:
    """Read-only connection to Minion 1's DB. Never written to."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        store._load_vec_extension(conn)  # only needed for the 768-dim adopt path
    except Exception:
        log.debug("vec extension not loaded on legacy ro conn", exc_info=True)
    return conn


def _legacy_embed_dim(ro: sqlite3.Connection) -> int:
    row = ro.execute("SELECT value FROM meta WHERE key='embed_dim'").fetchone()
    return int(row["value"]) if row and row["value"] else 384


def _legacy_chunk_count(ro: sqlite3.Connection) -> int:
    try:
        row = ro.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        return int(row["n"])
    except sqlite3.DatabaseError:
        return 0


def run(
    data_dir: Path,
    *,
    on_event: Optional[EventFn] = None,
    legacy_db: Optional[Path] = None,
    embed_dim: int = store.DEFAULT_EMBED_DIM,
    model_name: Optional[str] = None,
) -> dict:
    """Import Minion 1's vault into the Minion 2 vault at ``data_dir``.

    Returns a small status dict. Never raises — failures are logged and reported
    as ``{"status": "error"}`` so a bad import can't block sidecar startup.
    """
    emit = on_event or _noop
    try:
        return _run(data_dir, emit, legacy_db, embed_dim, model_name)
    except Exception:
        log.exception("legacy import failed")
        return {"status": "error"}


def _run(
    data_dir: Path,
    emit: EventFn,
    legacy_db: Optional[Path],
    embed_dim: int,
    model_name: Optional[str],
) -> dict:
    data_dir = Path(data_dir)
    new_db = data_dir / store.DB_FILENAME
    legacy_db = Path(legacy_db) if legacy_db else _legacy_db_path(data_dir)

    conn = store.connect(new_db, embed_dim=embed_dim)
    state = store.get_meta(conn, _STATE_KEY)
    if state == "done":
        return {"status": "already-imported"}

    resuming = state == "in_progress"

    if not resuming:
        # Only auto-import into an empty vault, and only if a legacy vault exists.
        if store.count_chunks(conn) > 0:
            return {"status": "skip", "reason": "vault not empty"}
        if not (legacy_db.exists() and legacy_db.stat().st_size > 0):
            return {"status": "skip", "reason": "no legacy db"}

        ro = _open_legacy_ro(legacy_db)
        try:
            legacy_count = _legacy_chunk_count(ro)
            if legacy_count == 0:
                return {"status": "skip", "reason": "legacy db empty"}
            legacy_dim = _legacy_embed_dim(ro)
            log.warning(
                "importing Minion 1 vault: %d chunks, legacy dim=%d -> %d",
                legacy_count, legacy_dim, embed_dim,
            )
            emit("batch_started", {"total": legacy_count})

            if legacy_dim == embed_dim:
                copied = _adopt_with_vectors(conn, ro)
                store.set_meta(conn, _STATE_KEY, "done")
                conn.commit()
                emit("batch_done", {"added": copied, "skipped": 0})
                return {"status": "adopted", "chunks": copied}

            _phase_a_copy_text(conn, ro)  # commits + sets state=in_progress
        finally:
            ro.close()

    # Phase B (also the resume entry point): re-embed chunks missing a vector.
    embedded = _phase_b_embed(conn, emit, model_name)
    store.set_meta(conn, _STATE_KEY, "done")
    conn.commit()
    total = store.count_chunks(conn)
    emit("batch_done", {"added": total, "skipped": 0})
    log.warning("legacy import complete: %d chunks (%d re-embedded)", total, embedded)
    return {"status": "imported", "chunks": total, "embedded": embedded}


def _phase_a_copy_text(conn: sqlite3.Connection, ro: sqlite3.Connection) -> None:
    """Copy every source + chunk (text/meta) in one transaction. FTS triggers
    light up keyword search immediately; vectors come later in Phase B."""
    sources = ro.execute(
        "SELECT source_id, path, kind, sha256, mtime, bytes, parser, meta_json, "
        "updated_at FROM sources"
    ).fetchall()
    chunks = ro.execute(
        "SELECT rowid, chunk_id, source_id, seq, role, text, meta_json FROM chunks"
    ).fetchall()

    with store.transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO sources("
            "source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (s["source_id"], s["path"], s["kind"], s["sha256"], s["mtime"],
                 s["bytes"], s["parser"], s["meta_json"], s["updated_at"])
                for s in sources
            ],
        )
        # Preserve rowid so Phase B can align vec_chunks rows to chunks. The
        # AFTER INSERT trigger populates fts_chunks from new.rowid/new.text.
        conn.executemany(
            "INSERT OR IGNORE INTO chunks("
            "rowid, chunk_id, source_id, seq, role, text, meta_json"
            ") VALUES(?,?,?,?,?,?,?)",
            [
                (c["rowid"], c["chunk_id"], c["source_id"], c["seq"], c["role"],
                 c["text"], c["meta_json"])
                for c in chunks
            ],
        )
        store.set_meta(conn, _STATE_KEY, "in_progress")
    log.warning("phase A: copied %d sources, %d chunks (text only)",
                len(sources), len(chunks))


def _adopt_with_vectors(conn: sqlite3.Connection, ro: sqlite3.Connection) -> int:
    """Same-dim legacy vault: copy sources, chunks AND vectors verbatim."""
    sources = ro.execute(
        "SELECT source_id, path, kind, sha256, mtime, bytes, parser, meta_json, "
        "updated_at FROM sources"
    ).fetchall()
    chunks = ro.execute(
        "SELECT rowid, chunk_id, source_id, seq, role, text, meta_json FROM chunks"
    ).fetchall()
    vecs = ro.execute("SELECT rowid, embedding FROM vec_chunks").fetchall()

    with store.transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO sources("
            "source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            [tuple(s) for s in sources],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO chunks("
            "rowid, chunk_id, source_id, seq, role, text, meta_json"
            ") VALUES(?,?,?,?,?,?,?)",
            [tuple(c) for c in chunks],
        )
        conn.executemany(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
            [(v["rowid"], v["embedding"]) for v in vecs],
        )
    log.warning("adopted same-dim legacy vault: %d chunks, %d vectors",
                len(chunks), len(vecs))
    return len(chunks)


def _phase_b_embed(
    conn: sqlite3.Connection,
    emit: EventFn,
    model_name: Optional[str],
) -> int:
    """Re-embed chunks that have no vector yet, newest source first. Commits per
    batch so an interrupted run resumes from where it stopped."""
    name = model_name or os.environ.get("MINION_EMBED_MODEL", ingest.DEFAULT_MODEL)

    pending: List[int] = [
        int(r["rowid"])
        for r in conn.execute(
            "SELECT c.rowid AS rowid FROM chunks c "
            "JOIN sources s ON s.source_id = c.source_id "
            "WHERE c.rowid NOT IN (SELECT rowid FROM vec_chunks) "
            "ORDER BY s.mtime DESC"
        ).fetchall()
    ]
    total = len(pending)
    if total == 0:
        return 0

    model = ingest._get_model(name)
    done = 0
    for start in range(0, total, _EMBED_BATCH):
        batch_ids = pending[start : start + _EMBED_BATCH]
        placeholders = ",".join("?" * len(batch_ids))
        rows = conn.execute(
            f"SELECT rowid, text FROM chunks WHERE rowid IN ({placeholders})",
            batch_ids,
        ).fetchall()
        texts = [r["text"] for r in rows]
        embeddings = store._l2_normalise(ingest._embed(model, texts))

        with store.transaction(conn):
            for r, emb in zip(rows, embeddings):
                conn.execute(
                    "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
                    (int(r["rowid"]), store._vec_blob(emb)),
                )
        done += len(rows)
        emit("file_progress", {"stage": "legacy_import", "done": done, "total": total})

    return done
