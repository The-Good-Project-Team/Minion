"""Re-embed an existing corpus under a new embedding model / dimension.

Switching the base embedder invalidates every stored vector (old model's
geometry != new model's). This rebuilds the `vec_chunks` table at the new dim
and re-embeds every chunk's text from the durable `chunks.text` column — the
text is never lost, only the vectors are recomputed.

Core routine `reindex_embeddings(conn, model_name)` operates on an open
connection. The `__main__` CLI adds a checkpoint + file backup so the live DB
is never mutated without a recoverable copy (the store has a corruption
history; we do not reindex in place without a backup).

Usage:
    PYTHONPATH=src python -m reindex --db /path/to/memory.db [--model NAME] [--no-backup]
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Dict

import numpy as np

log = logging.getLogger("minion.reindex")


def _model_dim(model) -> int:
    """Learn the model's output width by embedding one probe string."""
    vec = np.asarray(next(iter(model.embed(["dimension probe"]))), dtype=np.float32)
    return int(vec.shape[0])


def reindex_embeddings(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    batch_size: int = 64,
) -> Dict[str, int]:
    """Re-embed every chunk under `model_name`, rebuilding vec_chunks at its dim.

    Returns {"chunks", "dim"}. Destructive to vec_chunks; the caller is
    responsible for any backup. `chunks.text` (the source of truth) is untouched.
    """
    from ingest import _embed, _get_model
    from store import _ensure_vec_table, _l2_normalise, _vec_blob, meta_set

    model = _get_model(model_name)
    dim = _model_dim(model)

    rows = conn.execute("SELECT rowid, text FROM chunks ORDER BY rowid").fetchall()
    total = len(rows)
    log.info("reindex: %d chunks → %s (dim %d)", total, model_name, dim)

    # Rebuild the vec table at the new dim. Dropping the vec0 virtual table also
    # clears its shadow tables; recreate empty, then repopulate row-aligned.
    conn.execute("DROP TABLE IF EXISTS vec_chunks")
    _ensure_vec_table(conn, dim)

    # Pin the new model + dim so every later open and query agrees.
    meta_set(conn, "embed_dim", str(dim))
    meta_set(conn, "model_name", model_name)

    done = 0
    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        texts = [str(r["text"] or "") for r in batch]
        embs = _l2_normalise(_embed(model, texts).astype(np.float32, copy=False))
        with conn:
            for r, emb in zip(batch, embs):
                conn.execute(
                    "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
                    (int(r["rowid"]), _vec_blob(emb)),
                )
        done += len(batch)
        if done % (batch_size * 10) == 0 or done == total:
            log.info("reindex: %d/%d", done, total)

    return {"chunks": total, "dim": dim}


def _checkpoint_and_backup(db_path: Path) -> Path:
    """Fold WAL into the main file, then copy it to a timestamped backup."""
    import store

    conn = store.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    bak = db_path.with_name(f"{db_path.name}.reindex-bak-{int(time.time())}")
    shutil.copy2(db_path, bak)
    return bak


def main() -> None:
    import store
    from ingest import DEFAULT_MODEL

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Re-embed a minion corpus under a new model.")
    ap.add_argument("--db", required=True, help="Path to memory.db")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="fastembed model name")
    ap.add_argument("--no-backup", action="store_true", help="skip the safety backup")
    ap.add_argument("--batch-size", type=int, default=64)
    args = ap.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"no such DB: {db_path}")

    if not args.no_backup:
        bak = _checkpoint_and_backup(db_path)
        print(f"backup → {bak}")

    conn = store.connect(db_path)
    try:
        result = reindex_embeddings(conn, model_name=args.model, batch_size=args.batch_size)
    finally:
        conn.close()
    print(f"reindexed {result['chunks']} chunks at dim {result['dim']} under {args.model!r}")


if __name__ == "__main__":
    main()
