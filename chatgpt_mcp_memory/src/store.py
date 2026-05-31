"""
SQLite + sqlite-vec backed storage for Minion memory.

Schema
------
- sources(source_id PK, path UNIQUE, kind, sha256, mtime, bytes, parser, meta_json, updated_at)
- chunks(chunk_id PK, source_id FK ON DELETE CASCADE, seq, role, text, meta_json)
- vec_chunks (sqlite-vec virtual table): rowid -> embedding float[dim]

Invariants
----------
- chunks.rowid == vec_chunks.rowid for every live chunk (paired insert/delete).
- All multi-row writes happen inside a single transaction in upsert_source().
- sha256+mtime on sources lets the watcher skip unchanged files cheaply.

sqlite-vec is loaded via the `sqlite_vec` Python package's loadable extension.
See: https://github.com/asg017/sqlite-vec
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

try:
    import sqlite_vec  # type: ignore
except Exception:  # pragma: no cover - import guarded so docs-only installs still import
    sqlite_vec = None  # type: ignore


DEFAULT_EMBED_DIM = 384
DB_FILENAME = "memory.db"

log = logging.getLogger("minion.store")


# ---------------------------------------------------------------------------
# Dataclasses (mirror table rows, used in DAO signatures and return values)
# ---------------------------------------------------------------------------


@dataclass
class Source:
    source_id: str
    path: str
    kind: str
    sha256: str
    mtime: float
    bytes: int
    parser: str
    meta: Dict[str, Any]
    updated_at: float


@dataclass
class ChunkRow:
    chunk_id: str
    source_id: str
    seq: int
    role: Optional[str]
    text: str
    meta: Dict[str, Any]


@dataclass
class Hit:
    chunk_id: str
    score: float
    text: str
    role: Optional[str]
    source_id: str
    path: str
    kind: str
    mtime: float
    meta: Dict[str, Any]
    source_meta: Dict[str, Any]
    storage_tier: str = "hot"


# ---------------------------------------------------------------------------
# Connection + schema bootstrap
# ---------------------------------------------------------------------------


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
    source_id   TEXT PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    mtime       REAL NOT NULL,
    bytes       INTEGER NOT NULL,
    parser      TEXT NOT NULL,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_kind ON sources(kind);
CREATE INDEX IF NOT EXISTS idx_sources_mtime ON sources(mtime);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    role        TEXT,
    text        TEXT NOT NULL,
    meta_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_role ON chunks(role);

-- Expression indices over chunk meta_json for temporal + per-conversation queries.
-- Idempotent; applied on every connect() so old DBs upgrade in-place.
CREATE INDEX IF NOT EXISTS idx_chunks_create_time
    ON chunks(json_extract(meta_json, '$.create_time'));
CREATE INDEX IF NOT EXISTS idx_chunks_conv_id
    ON chunks(json_extract(meta_json, '$.conversation_id'));

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_claims (
    claim_id      TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'proposed',
    confidence    REAL,
    source_agent  TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    superseded_by TEXT,
    meta_json     TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (superseded_by) REFERENCES identity_claims(claim_id)
);

CREATE INDEX IF NOT EXISTS idx_identity_claims_status ON identity_claims(status);
CREATE INDEX IF NOT EXISTS idx_identity_claims_kind ON identity_claims(kind);
CREATE INDEX IF NOT EXISTS idx_identity_claims_updated ON identity_claims(updated_at);

CREATE TABLE IF NOT EXISTS identity_edges (
    edge_id     TEXT PRIMARY KEY,
    claim_id    TEXT NOT NULL REFERENCES identity_claims(claim_id) ON DELETE CASCADE,
    chunk_id    TEXT REFERENCES chunks(chunk_id) ON DELETE SET NULL,
    source_id   TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
    rationale   TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identity_edges_claim ON identity_edges(claim_id);
CREATE INDEX IF NOT EXISTS idx_identity_edges_chunk ON identity_edges(chunk_id);

CREATE TABLE IF NOT EXISTS preference_clusters (
    cluster_id              TEXT PRIMARY KEY,
    label                   TEXT NOT NULL,
    summary                 TEXT NOT NULL,
    member_chunk_ids_json   TEXT NOT NULL,
    run_at                  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_preference_clusters_run ON preference_clusters(run_at);
"""


_FTS_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks
    USING fts5(text, content='chunks', content_rowid='rowid',
               tokenize='porter unicode61 remove_diacritics 2');

CREATE TRIGGER IF NOT EXISTS chunks_ai_fts AFTER INSERT ON chunks BEGIN
    INSERT INTO fts_chunks(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad_fts AFTER DELETE ON chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES('delete', old.rowid, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au_fts AFTER UPDATE ON chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES('delete', old.rowid, old.text);
    INSERT INTO fts_chunks(rowid, text) VALUES (new.rowid, new.text);
END;
"""


# Tears down the FTS5 index + its sync triggers so it can be recreated from the
# `chunks` content table. The index is derived data: dropping it never loses
# source text. Used by FTS-only corruption recovery.
_FTS_DROP_SQL = """
DROP TRIGGER IF EXISTS chunks_ai_fts;
DROP TRIGGER IF EXISTS chunks_ad_fts;
DROP TRIGGER IF EXISTS chunks_au_fts;
DROP TABLE IF EXISTS fts_chunks;
"""


def _apply_schema_upgrades(conn: sqlite3.Connection) -> None:
    """Idempotent migrations layered onto `_SCHEMA_SQL` for older installs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ambient_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            captured_at REAL NOT NULL,
            sensitivity TEXT NOT NULL DEFAULT 'vault_local',
            payload_json TEXT NOT NULL DEFAULT '{}',
            storage_tier TEXT NOT NULL DEFAULT 'hot',
            ttl_expires_at REAL,
            dedupe_key TEXT UNIQUE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ambient_events_captured ON ambient_events(captured_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screen_memory_events (
            event_id TEXT PRIMARY KEY,
            occurred_at REAL NOT NULL,
            app TEXT NOT NULL DEFAULT '',
            window TEXT NOT NULL DEFAULT '',
            url TEXT,
            scene TEXT NOT NULL DEFAULT '',
            visible_elements_json TEXT NOT NULL DEFAULT '[]',
            actions_json TEXT NOT NULL DEFAULT '[]',
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.0,
            trust_tier TEXT NOT NULL DEFAULT 'unknown',
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_screen_memory_events_time ON screen_memory_events(occurred_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_screen_memory_events_app ON screen_memory_events(app, occurred_at DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS identity_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            action TEXT NOT NULL,
            claim_id TEXT,
            detail_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_identity_audit_ts ON identity_audit_log(ts DESC)"
    )

    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if cols and "storage_tier" not in cols:
        conn.execute(
            "ALTER TABLE chunks ADD COLUMN storage_tier TEXT NOT NULL DEFAULT 'hot'"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_pages (
            page_id TEXT PRIMARY KEY,
            page_type TEXT NOT NULL,
            title TEXT NOT NULL,
            body_md TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            last_updated REAL NOT NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            storage_tier TEXT NOT NULL DEFAULT 'hot'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wiki_pages_type ON wiki_pages(page_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wiki_pages_status ON wiki_pages(status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_links (
            link_id TEXT PRIMARY KEY,
            from_page_id TEXT NOT NULL REFERENCES wiki_pages(page_id) ON DELETE CASCADE,
            to_page_id TEXT NOT NULL REFERENCES wiki_pages(page_id) ON DELETE CASCADE,
            link_kind TEXT NOT NULL DEFAULT 'related',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wiki_links_from ON wiki_links(from_page_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            origin TEXT NOT NULL DEFAULT 'inferred',
            priority TEXT,
            due_at REAL,
            body_md TEXT NOT NULL DEFAULT '',
            context_refs_json TEXT NOT NULL DEFAULT '[]',
            wiki_refs_json TEXT NOT NULL DEFAULT '[]',
            output_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_origin ON tasks(origin)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outputs (
            output_id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
            kind TEXT NOT NULL DEFAULT 'draft',
            body_md TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            wiki_read_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_sources (
            source_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'healthy',
            last_sync_at REAL,
            config_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_job_runs (
            run_id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL,
            items_count INTEGER NOT NULL DEFAULT 0,
            error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_job_runs_started ON sync_job_runs(started_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_issues (
            issue_id TEXT PRIMARY KEY,
            severity TEXT NOT NULL DEFAULT 'warning',
            source_key TEXT,
            body_md TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at REAL NOT NULL,
            resolved_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_system_issues_status ON system_issues(status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_nodes (
            node_id TEXT PRIMARY KEY,
            node_kind TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'stub',
            body_md TEXT NOT NULL DEFAULT '',
            wiki_page_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_nodes_kind ON graph_nodes(node_kind)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_edges (
            edge_id TEXT PRIMARY KEY,
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            rel_kind TEXT NOT NULL DEFAULT 'related',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_edges_from ON graph_edges(from_node_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_candidates (
            candidate_id TEXT PRIMARY KEY,
            candidate_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            title TEXT NOT NULL DEFAULT '',
            body_md TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.0,
            source TEXT NOT NULL DEFAULT 'graph',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            resolved_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_candidates_status ON graph_candidates(status, updated_at DESC)"
    )
    _migrate_graph_schema(conn)
    _migrate_council_schema(conn)
    from chat_store import _migrate_chat_schema

    _migrate_chat_schema(conn)

    seed_sync_sources(conn)
    seed_graph_scaffold(conn)
    conn.commit()


def _migrate_graph_schema(conn: sqlite3.Connection) -> None:
    """Layer new life-graph columns onto older graph_nodes / graph_edges installs."""
    node_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(graph_nodes)").fetchall()}
    if node_cols:
        adds = [
            ("parent_node_id", "TEXT"),
            ("aliases_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("summary", "TEXT NOT NULL DEFAULT ''"),
            ("confidence", "REAL NOT NULL DEFAULT 0.0"),
            ("source_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("privacy_level", "TEXT NOT NULL DEFAULT 'vault_local'"),
        ]
        for col, ddl in adds:
            if col not in node_cols:
                conn.execute(f"ALTER TABLE graph_nodes ADD COLUMN {col} {ddl}")
    edge_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(graph_edges)").fetchall()}
    if edge_cols:
        for col, ddl in [
            ("confidence", "REAL NOT NULL DEFAULT 0.0"),
            ("source_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("updated_at", "REAL"),
        ]:
            if col not in edge_cols:
                conn.execute(f"ALTER TABLE graph_edges ADD COLUMN {col} {ddl}")


def _migrate_council_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS council_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            domain TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            pattern_id TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            detected_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_council_events_detected ON council_events(detected_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS council_proposals (
            proposal_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            proposal_type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            intensity TEXT NOT NULL DEFAULT 'standard',
            required_skill TEXT NOT NULL,
            required_info_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',
            pattern_id TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_council_proposals_open "
        "ON council_proposals(status, intensity, updated_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS council_approvals (
            approval_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            action TEXT NOT NULL,
            snooze_until REAL,
            edited_payload_json TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS council_pattern_state (
            pattern_id TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            learned_cadence_days REAL,
            suppress_until REAL,
            reject_count INTEGER NOT NULL DEFAULT 0,
            last_approved_at REAL,
            last_signal_at REAL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (pattern_id, subject_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_refs (
            cap_key TEXT NOT NULL,
            ref_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            vault_ref TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_capability_refs_key ON capability_refs(cap_key, status)"
    )


def _ensure_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    """Create the sqlite-vec virtual table if missing. Dim is baked into the DDL."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
    ).fetchone()
    if row:
        return
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{dim}])"
    )


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    if sqlite_vec is None:
        raise RuntimeError(
            "sqlite-vec is not installed. Install with `pip install sqlite-vec`."
        )
    if not hasattr(conn, "enable_load_extension"):
        raise RuntimeError(
            "This Python was built without SQLite extension loading "
            "(PEP 524 flag --enable-loadable-sqlite-extensions). "
            "macOS system Python 3.9 ships this way. Use uv's Python 3.11 "
            "(`uv python install 3.11 && uv venv --python 3.11`) or a "
            "Homebrew/pyenv Python built with extension loading enabled."
        )
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _journal_mode_from_env() -> Optional[str]:
    raw = (os.environ.get("MINION_SQLITE_JOURNAL") or "").strip().lower()
    if raw in ("wal", "delete"):
        return raw
    return None


def _wal_shm_paths(db_path: Path) -> tuple[Path, Path]:
    """Sidecar files SQLite uses with WAL (same directory, `-wal` / `-shm` suffix)."""
    parent = db_path.parent
    name = db_path.name
    return (parent / f"{name}-wal", parent / f"{name}-shm")


def _safe_unlink_wal_shm(db_path: Path) -> list[str]:
    """Remove WAL companions when the main DB is closed. Returns removed paths for logs."""
    removed: list[str] = []
    for p in _wal_shm_paths(db_path):
        try:
            if p.is_file():
                p.unlink()
                removed.append(str(p))
        except OSError as e:
            log.warning("could not remove %s: %s", p, e)
    return removed


def _db_looks_corrupt(db_path: Path, err: Optional[BaseException]) -> bool:
    """Only rotate aside when corruption is likely — not on transient lock/I/O blips."""
    if os.environ.get("MINION_SQLITE_ROTATE_ON_FAILURE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True
    err_s = (str(err) if err else "").lower()
    if any(
        x in err_s
        for x in (
            "malformed",
            "corrupt",
            "not a database",
            "disk image",
            "quick_check",
            "file is not a database",
        )
    ):
        return True
    if not db_path.exists() or db_path.stat().st_size < 512:
        return False
    try:
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = ro.execute("PRAGMA quick_check").fetchone()
        ro.close()
        if row is None:
            return True
        return str(row[0]).lower() != "ok"
    except sqlite3.Error:
        return True
    except OSError:
        return False


def _rotate_corrupt_database(db_path: Path) -> Path:
    """Rename the main DB aside so the next open creates a fresh file. Returns backup path."""
    backup = db_path.with_name(f"{db_path.name}.corrupt.{int(time.time())}.bak")
    if db_path.exists():
        try:
            shutil.move(str(db_path), str(backup))
        except OSError as e:
            raise sqlite3.OperationalError(
                f"Could not move aside corrupt database {db_path}: {e}"
            ) from e
    _safe_unlink_wal_shm(db_path)
    try:
        flag = db_path.parent / ".last_db_rotate.json"
        flag.write_text(
            json.dumps(
                {
                    "backup_path": str(backup),
                    "rotated_at": time.time(),
                    "db_path": str(db_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        log.warning("could not write last_db_rotate flag", exc_info=True)
    return backup


def _err_indicates_corruption(err: Optional[BaseException]) -> bool:
    """True for any on-disk corruption signal (not a transient lock/I/O blip).

    The exact phrasing varies by where SQLite first trips over the damage — a
    contentless FTS5 index can surface as the specific "malformed inverted index"
    message from ``quick_check`` or as a generic "database disk image is
    malformed" from the first query that touches it. We can't tell FTS-only
    damage from base-table damage by message alone, so on any corruption signal
    we let the recovery loop attempt the (safe, self-declining) FTS rebuild
    before escalating to a whole-DB rotation.
    """
    s = (str(err) if err else "").lower()
    return any(
        x in s
        for x in ("malformed", "corrupt", "not a database", "disk image")
    )


def _repair_fts_index(db_path: Path) -> bool:
    """Rebuild a corrupt FTS5 index in place. Returns True only if the whole DB
    passes ``quick_check`` afterwards.

    Precondition check: the base `chunks` table must read cleanly. If it does
    not, the damage is deeper than the index and we let the caller fall through
    to whole-DB rotation. Because the FTS table is contentless-linked
    (``content='chunks'``), dropping and rebuilding it never loses source text.
    """
    _safe_unlink_wal_shm(db_path)
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=8000")
        except sqlite3.OperationalError:
            pass
        _load_vec_extension(conn)
        try:
            conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        except sqlite3.DatabaseError as e:
            log.warning("FTS repair aborted: base `chunks` table unreadable (%s)", e)
            return False
        conn.executescript(_FTS_DROP_SQL)
        conn.executescript(_FTS_SCHEMA_SQL)
        with transaction(conn):
            conn.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')")
        _verify_connection(conn)
        conn.commit()
        return True
    except sqlite3.DatabaseError as e:
        log.warning("FTS repair failed for %s: %s", db_path, e)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _apply_journal_mode(
    conn: sqlite3.Connection, db_path: Path, *, wal_first: bool = True
) -> str:
    """Pick WAL or DELETE journal; WAL may be unsupported on some volumes."""
    forced = _journal_mode_from_env()
    if forced == "delete":
        row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        return str(row[0]) if row else "delete"
    if forced == "wal":
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        return str(row[0]) if row else "wal"

    if not wal_first:
        try:
            row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            return str(row[0]) if row else "delete"
        except sqlite3.OperationalError as e2:
            hint = (
                f"SQLite cannot set DELETE journal at {db_path}: {e2}. "
                "Check free disk space; avoid iCloud or network-backed paths; "
                "set MINION_DATA_DIR to a local folder."
            )
            raise sqlite3.OperationalError(hint) from e2

    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        got = str(row[0]) if row else ""
        if got.upper() != "WAL":
            log.warning("journal_mode requested WAL got %s for %s", got or "?", db_path)
        return got or "wal"
    except sqlite3.OperationalError as e:
        log.warning(
            "WAL unavailable for %s (%s); falling back to DELETE journal",
            db_path,
            e,
        )
        try:
            row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            return str(row[0]) if row else "delete"
        except sqlite3.OperationalError as e2:
            hint = (
                f"SQLite cannot configure journal at {db_path}: {e2}. "
                "Check free disk space; avoid iCloud or network-backed paths; "
                "set MINION_DATA_DIR to a local folder; quit Minion and remove "
                "stale memory.db-wal / memory.db-shm if the DB was uncleanly closed."
            )
            raise sqlite3.OperationalError(hint) from e2


def _verify_connection(conn: sqlite3.Connection) -> None:
    """Fail closed if the DB file is not actually usable (quick_check + ping)."""
    conn.execute("SELECT 1").fetchone()
    row = conn.execute("PRAGMA quick_check").fetchone()
    if row is None:
        raise sqlite3.OperationalError("PRAGMA quick_check returned no row")
    result = str(row[0]).lower()
    if result != "ok":
        raise sqlite3.OperationalError(f"PRAGMA quick_check: {row[0]}")


def _bootstrap_schema(conn: sqlite3.Connection, embed_dim: int) -> None:
    conn.executescript(_SCHEMA_SQL)
    _ensure_vec_table(conn, embed_dim)
    _ensure_fts_table(conn)
    _apply_schema_upgrades(conn)

    existing = conn.execute("SELECT value FROM meta WHERE key='embed_dim'").fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)", ("embed_dim", str(embed_dim))
        )
        conn.commit()


def _open_and_prepare(
    db_path: Path,
    embed_dim: int,
    *,
    wal_first: bool,
    synchronous: str,
) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA busy_timeout=8000")
        except sqlite3.OperationalError:
            pass
        _load_vec_extension(conn)
        _apply_journal_mode(conn, db_path, wal_first=wal_first)
        conn.execute("PRAGMA foreign_keys=ON")
        if synchronous.upper() == "FULL":
            conn.execute("PRAGMA synchronous=FULL")
        else:
            conn.execute("PRAGMA synchronous=NORMAL")
        _bootstrap_schema(conn, embed_dim)
        _verify_connection(conn)
        return conn
    except BaseException:
        try:
            conn.close()
        except Exception:
            pass
        raise


def connect(db_path: Path, *, embed_dim: int = DEFAULT_EMBED_DIM) -> sqlite3.Connection:
    """
    Open (creating if needed) the memory DB, load sqlite-vec, apply schema.

    On ``OperationalError`` (common: disk I/O, stale WAL), runs a short ordered
    self-recovery sequence (remove ``-wal``/``-shm``, force DELETE journal,
    stricter sync, then rotate a corrupt DB aside) and re-verifies with
    ``PRAGMA quick_check`` after each successful open.

    `embed_dim` is only used when creating vec_chunks for the first time; once
    set, the DB is locked to that dimension until dropped/recreated.
    """
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # (wal_first, synchronous, pre_action) — pre_action runs before this attempt.
    # Ordered most-likely fix first; each successful open ends with quick_check.
    recovery_plan: list[tuple[bool, str, Optional[str]]] = [
        (True, "NORMAL", None),
        (True, "NORMAL", "unlink_wal_shm"),
        (False, "NORMAL", "unlink_wal_shm"),
        (False, "FULL", "unlink_wal_shm"),
        (True, "NORMAL", "rebuild_fts"),
        (True, "NORMAL", "rotate_db"),
    ]

    last_err: Optional[BaseException] = None
    corruption_seen = False
    for pass_i, (wal_first, sync_mode, pre) in enumerate(recovery_plan):
        if pre == "rebuild_fts":
            # The FTS5 index is derived from `chunks`; repair it in place rather
            # than rotating the whole vault aside. _repair_fts_index is the
            # authority: it declines (returns False) unless `chunks` reads
            # cleanly and the rebuilt DB passes quick_check, so attempting it on
            # any corruption signal is safe and never costs data.
            if not (corruption_seen and _repair_fts_index(db_path)):
                continue
            log.error(
                "SQLite recovery pass %s: rebuilt corrupt FTS index in place for %s "
                "(captured memory preserved; no rotation needed)",
                pass_i,
                db_path,
            )
        elif pre == "unlink_wal_shm":
            removed = _safe_unlink_wal_shm(db_path)
            if removed:
                log.warning(
                    "SQLite recovery pass %s: removed WAL side files %s",
                    pass_i,
                    removed,
                )
        elif pre == "rotate_db":
            err_s = (str(last_err) if last_err else "").lower()
            if any(
                x in err_s
                for x in ("disk full", "no space left", "enospc", "sqlite_full", "database or disk is full")
            ):
                log.error("Skipping corrupt-database rotate (likely disk full): %s", last_err)
                break
            if db_path.exists() and not _db_looks_corrupt(db_path, last_err):
                log.error(
                    "Skipping automatic DB rotate (graph/data preserved); "
                    "transient open error: %s. Set MINION_SQLITE_ROTATE_ON_FAILURE=1 to force.",
                    last_err,
                )
                break
            if db_path.exists():
                backup = _rotate_corrupt_database(db_path)
                log.error(
                    "SQLite recovery pass %s: moved unreadable DB aside to %s "
                    "(fresh empty database created; re-index from inbox/exports)",
                    pass_i,
                    backup,
                )
            else:
                _safe_unlink_wal_shm(db_path)

        try:
            conn = _open_and_prepare(
                db_path, embed_dim, wal_first=wal_first, synchronous=sync_mode
            )
            if pass_i > 0:
                log.warning(
                    "SQLite connect succeeded for %s after recovery pass %s "
                    "(wal_first=%s sync=%s pre=%s)",
                    db_path,
                    pass_i,
                    wal_first,
                    sync_mode,
                    pre,
                )
            return conn
        except RuntimeError:
            raise
        except sqlite3.DatabaseError as e:
            last_err = e
            if _err_indicates_corruption(e):
                corruption_seen = True
            log.warning(
                "SQLite open failed pass %s (%s wal_first=%s sync=%s pre=%s): %s",
                pass_i,
                db_path,
                wal_first,
                sync_mode,
                pre,
                e,
            )
        except OSError as e:
            last_err = e
            log.warning(
                "SQLite open failed pass %s (OSError) for %s: %s",
                pass_i,
                db_path,
                e,
            )

    assert last_err is not None
    raise last_err


def _ensure_fts_table(conn: sqlite3.Connection) -> None:
    """Create FTS5 table + triggers; backfill from existing chunks on first open.

    Contentless-linked FTS5 (content='chunks') so we never double-store text.
    Backfill is one-time: idempotent guard by row-count comparison.
    """
    try:
        conn.executescript(_FTS_SCHEMA_SQL)
    except sqlite3.OperationalError as e:
        # FTS5 not compiled in — log via meta and skip. Keyword mode will error
        # at query time with a clear message.
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("fts_unavailable", str(e)),
        )
        conn.commit()
        return

    # Lazy backfill: if fts has fewer rows than chunks, rebuild.
    try:
        n_chunks = int(conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
        n_fts = int(conn.execute("SELECT COUNT(*) AS n FROM fts_chunks").fetchone()["n"])
    except sqlite3.OperationalError:
        return
    if n_chunks > 0 and n_fts < n_chunks:
        with transaction(conn):
            conn.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')")


def fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_chunks'"
    ).fetchone()
    return row is not None


def get_embed_dim(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='embed_dim'").fetchone()
    return int(row["value"]) if row else DEFAULT_EMBED_DIM


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def source_id_for(path: str) -> str:
    """Stable ID from the absolute path. Same path across runs => same source_id."""
    return "src-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def chunk_id_for(source_id: str, seq: int) -> str:
    return f"{source_id}:{seq:06d}"


def sha256_of_file(path: Path, *, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(bufsize)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _l2_normalise(vec: np.ndarray) -> np.ndarray:
    if vec.ndim == 1:
        n = float(np.linalg.norm(vec))
        return vec if n == 0.0 else vec / n
    norms = np.linalg.norm(vec, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vec / norms


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`with transaction(conn) as c:` — commits on success, rolls back on raise.

    Uses a SAVEPOINT when already inside a transaction (ambient tick + upsert_source).
    """
    nested = bool(getattr(conn, "in_transaction", False))
    if nested:
        conn.execute("SAVEPOINT minion_sp")
        try:
            yield conn
            conn.execute("RELEASE SAVEPOINT minion_sp")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT minion_sp")
            conn.execute("RELEASE SAVEPOINT minion_sp")
            raise
        return
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# DAO
# ---------------------------------------------------------------------------


def get_source_by_path(conn: sqlite3.Connection, path: str) -> Optional[Source]:
    row = conn.execute(
        "SELECT source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at "
        "FROM sources WHERE path=?",
        (path,),
    ).fetchone()
    return _row_to_source(row) if row else None


def get_source(conn: sqlite3.Connection, source_id: str) -> Optional[Source]:
    row = conn.execute(
        "SELECT source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at "
        "FROM sources WHERE source_id=?",
        (source_id,),
    ).fetchone()
    return _row_to_source(row) if row else None


def list_sources(
    conn: sqlite3.Connection,
    *,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    sql = [
        "SELECT s.source_id, s.path, s.kind, s.sha256, s.mtime, s.bytes, s.parser, "
        "s.meta_json, s.updated_at, "
        "(SELECT COUNT(*) FROM chunks c WHERE c.source_id=s.source_id) AS chunk_count "
        "FROM sources s WHERE 1=1"
    ]
    params: List[Any] = []
    if kind:
        sql.append("AND s.kind=?")
        params.append(kind)
    if path_glob:
        sql.append("AND s.path GLOB ?")
        params.append(path_glob)
    if since is not None:
        sql.append("AND s.mtime >= ?")
        params.append(float(since))
    sql.append("ORDER BY s.mtime DESC LIMIT ?")
    params.append(int(limit))

    rows = conn.execute(" ".join(sql), params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "source_id": r["source_id"],
                "path": r["path"],
                "kind": r["kind"],
                "sha256": r["sha256"],
                "mtime": r["mtime"],
                "bytes": r["bytes"],
                "parser": r["parser"],
                "meta": json.loads(r["meta_json"] or "{}"),
                "updated_at": r["updated_at"],
                "chunk_count": int(r["chunk_count"]),
            }
        )
    return out


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        source_id=row["source_id"],
        path=row["path"],
        kind=row["kind"],
        sha256=row["sha256"],
        mtime=row["mtime"],
        bytes=row["bytes"],
        parser=row["parser"],
        meta=json.loads(row["meta_json"] or "{}"),
        updated_at=row["updated_at"],
    )


def delete_source(conn: sqlite3.Connection, source_id: str) -> int:
    """Delete a source + cascade chunks + vec rows. Returns chunks removed."""
    with transaction(conn):
        rows = conn.execute(
            "SELECT rowid FROM chunks WHERE source_id=?", (source_id,)
        ).fetchall()
        rowids = [int(r["rowid"]) for r in rows]
        for rid in rowids:
            conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (rid,))
        conn.execute("DELETE FROM sources WHERE source_id=?", (source_id,))
    return len(rowids)


def delete_source_by_path(conn: sqlite3.Connection, path: str) -> int:
    src = get_source_by_path(conn, path)
    if src is None:
        return 0
    return delete_source(conn, src.source_id)


def delete_sources_by_kind(conn: sqlite3.Connection, kind: str) -> Tuple[int, int]:
    """Remove every source of ``kind``. Returns ``(sources_removed, chunks_removed)``."""
    rows = conn.execute(
        "SELECT source_id FROM sources WHERE kind=?", (kind,)
    ).fetchall()
    ids = [str(r["source_id"]) for r in rows]
    chunk_total = 0
    for sid in ids:
        chunk_total += delete_source(conn, sid)
    return len(ids), chunk_total


def upsert_source(
    conn: sqlite3.Connection,
    *,
    path: str,
    kind: str,
    sha256: str,
    mtime: float,
    bytes_: int,
    parser: str,
    source_meta: Dict[str, Any],
    chunks: Sequence[Tuple[str, Optional[str], Dict[str, Any]]],
    embeddings: np.ndarray,
) -> str:
    """
    Replace a source and all its chunks atomically.

    chunks: sequence of (text, role, chunk_meta) in seq order.
    embeddings: (N, dim) float32, row-aligned with chunks. Will be L2-normalised.
    Returns source_id.
    """
    if len(chunks) != embeddings.shape[0]:
        raise ValueError(
            f"chunks/embeddings length mismatch: {len(chunks)} vs {embeddings.shape[0]}"
        )

    expected_dim = get_embed_dim(conn)
    if embeddings.size and embeddings.shape[1] != expected_dim:
        raise ValueError(
            f"embedding dim mismatch: got {embeddings.shape[1]}, db expects {expected_dim}"
        )

    embeddings = _l2_normalise(embeddings.astype(np.float32, copy=False))
    sid = source_id_for(path)
    now = time.time()

    with transaction(conn):
        # Wipe prior rows (cascade clears chunks, but we still need vec cleanup).
        prior = conn.execute(
            "SELECT rowid FROM chunks WHERE source_id=?", (sid,)
        ).fetchall()
        for r in prior:
            conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (int(r["rowid"]),))
        conn.execute("DELETE FROM sources WHERE source_id=?", (sid,))

        conn.execute(
            "INSERT INTO sources(source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                path,
                kind,
                sha256,
                float(mtime),
                int(bytes_),
                parser,
                json.dumps(source_meta, ensure_ascii=False),
                now,
            ),
        )

        for seq, ((text, role, cmeta), emb) in enumerate(zip(chunks, embeddings)):
            cid = chunk_id_for(sid, seq)
            cur = conn.execute(
                "INSERT INTO chunks(chunk_id, source_id, seq, role, text, meta_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (cid, sid, seq, role, text, json.dumps(cmeta, ensure_ascii=False)),
            )
            rid = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
                (rid, _vec_blob(emb)),
            )
    return sid


def _vec_blob(vec: np.ndarray) -> bytes:
    # sqlite-vec accepts raw little-endian float32 bytes for float[N] columns.
    return np.ascontiguousarray(vec, dtype=np.float32).tobytes()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def search(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    *,
    top_k: int = 8,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    since: Optional[float] = None,
    before: Optional[float] = None,
    role: Optional[str] = None,
) -> List[Hit]:
    """KNN search with optional source/role filters.

    To keep the filtering cheap we over-fetch KNN candidates (top_k * 8) and
    then filter+trim in SQL. For <100k chunks this is well under a ms.
    """
    q = _l2_normalise(query_vec.astype(np.float32, copy=False))
    expected = get_embed_dim(conn)
    if q.shape[0] != expected:
        raise ValueError(f"query dim mismatch: got {q.shape[0]}, db expects {expected}")

    fetch = max(top_k * 8, top_k + 16)
    cand = conn.execute(
        "SELECT rowid, distance FROM vec_chunks "
        "WHERE embedding MATCH ? AND k=? "
        "ORDER BY distance",
        (_vec_blob(q), fetch),
    ).fetchall()
    if not cand:
        return []

    rowids = [int(r["rowid"]) for r in cand]
    dist_by_rowid = {int(r["rowid"]): float(r["distance"]) for r in cand}

    placeholders = ",".join("?" * len(rowids))
    sql = [
        f"SELECT c.rowid AS rid, c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        f"s.path, s.kind, s.mtime, s.meta_json AS source_meta_json, "
        f"COALESCE(c.storage_tier, 'hot') AS storage_tier "
        f"FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        f"WHERE c.rowid IN ({placeholders})"
    ]
    params: List[Any] = list(rowids)
    if role:
        sql.append("AND c.role=?")
        params.append(role)
    if kind:
        sql.append("AND s.kind=?")
        params.append(kind)
    if path_glob:
        sql.append("AND s.path GLOB ?")
        params.append(path_glob)
    if since is not None:
        sql.append("AND s.mtime >= ?")
        params.append(float(since))
    if before is not None:
        sql.append("AND s.mtime <= ?")
        params.append(float(before))

    rows = conn.execute(" ".join(sql), params).fetchall()
    # Preserve vec-order, then cap.
    ordered = sorted(rows, key=lambda r: dist_by_rowid[int(r["rid"])])
    hits: List[Hit] = []
    for r in ordered[:top_k]:
        dist = dist_by_rowid[int(r["rid"])]
        # vec0's default distance is L2. Because we store L2-normalised vectors,
        # cos_sim = 1 - dist^2 / 2, which maps to [-1, 1] and matches the
        # cosine-similarity convention callers expect.
        score = 1.0 - (dist * dist) / 2.0
        hits.append(
            Hit(
                chunk_id=r["chunk_id"],
                score=score,
                text=r["text"],
                role=r["role"],
                source_id=r["source_id"],
                path=r["path"],
                kind=r["kind"],
                mtime=r["mtime"],
                meta=json.loads(r["meta_json"] or "{}"),
                source_meta=json.loads(r["source_meta_json"] or "{}"),
                storage_tier=str(r["storage_tier"] or "hot"),
            )
        )
    return hits


def get_chunk(conn: sqlite3.Connection, chunk_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        "COALESCE(c.storage_tier, 'hot') AS storage_tier, "
        "s.path, s.kind, s.mtime, s.meta_json AS source_meta_json "
        "FROM chunks c JOIN sources s ON s.source_id=c.source_id "
        "WHERE c.chunk_id=?",
        (chunk_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "chunk_id": row["chunk_id"],
        "source_id": row["source_id"],
        "role": row["role"],
        "text": row["text"],
        "path": row["path"],
        "kind": row["kind"],
        "mtime": row["mtime"],
        "storage_tier": str(row["storage_tier"] or "hot"),
        "meta": json.loads(row["meta_json"] or "{}"),
        "source_meta": json.loads(row["source_meta_json"] or "{}"),
    }


def browse_chunks_chronological(
    conn: sqlite3.Connection,
    *,
    order: str = "oldest",
    role: Optional[str] = None,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    before: Optional[float] = None,
    after: Optional[float] = None,
    query_substring: Optional[str] = None,
    limit: int = 10,
) -> List[Hit]:
    """Pure-SQL chronological retrieval over chunks by meta.create_time.

    No embedding. Drops hits with missing create_time so results are always sorted.
    `query_substring` is an optional case-insensitive LIKE filter on chunk.text.
    """
    if order not in ("oldest", "newest"):
        raise ValueError(f"order must be 'oldest' or 'newest', got {order!r}")
    direction = "ASC" if order == "oldest" else "DESC"

    sql = [
        "SELECT c.rowid AS rid, c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        "s.path, s.kind, s.mtime, s.meta_json AS source_meta_json, "
        "COALESCE(c.storage_tier, 'hot') AS storage_tier, "
        "json_extract(c.meta_json, '$.create_time') AS ctime "
        "FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        "WHERE json_extract(c.meta_json, '$.create_time') IS NOT NULL"
    ]
    params: List[Any] = []
    if role:
        sql.append("AND c.role=?")
        params.append(role)
    if kind:
        sql.append("AND s.kind=?")
        params.append(kind)
    if path_glob:
        sql.append("AND s.path GLOB ?")
        params.append(path_glob)
    if before is not None:
        sql.append("AND json_extract(c.meta_json, '$.create_time') <= ?")
        params.append(float(before))
    if after is not None:
        sql.append("AND json_extract(c.meta_json, '$.create_time') >= ?")
        params.append(float(after))
    if query_substring:
        sql.append("AND LOWER(c.text) LIKE ?")
        params.append(f"%{query_substring.lower()}%")
    sql.append(f"ORDER BY ctime {direction} LIMIT ?")
    params.append(int(max(1, limit)))

    rows = conn.execute(" ".join(sql), params).fetchall()
    hits: List[Hit] = []
    for r in rows:
        hits.append(
            Hit(
                chunk_id=r["chunk_id"],
                # Score convention: for chronological results we use a recency
                # proxy in (0, 1]. 1.0 = rank-1, decreasing monotonically.
                score=1.0,
                text=r["text"],
                role=r["role"],
                source_id=r["source_id"],
                path=r["path"],
                kind=r["kind"],
                mtime=r["mtime"],
                meta=json.loads(r["meta_json"] or "{}"),
                source_meta=json.loads(r["source_meta_json"] or "{}"),
                storage_tier=str(r["storage_tier"] or "hot"),
            )
        )
    return hits


def keyword_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 8,
    role: Optional[str] = None,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    before: Optional[float] = None,
    after: Optional[float] = None,
) -> List[Hit]:
    """FTS5 BM25-ranked keyword search over chunk text.

    `query` is passed through to FTS5 MATCH; callers should pre-quote phrases
    with internal whitespace to avoid spurious operator parsing.
    """
    if not fts_available(conn):
        raise RuntimeError(
            "FTS5 table not available; this SQLite build may lack FTS5 support. "
            "Rebuild the index or use mode='relevance'."
        )
    if not query or not query.strip():
        return []

    sql = [
        "SELECT c.chunk_id, c.source_id, c.role, c.text, c.meta_json, "
        "s.path, s.kind, s.mtime, s.meta_json AS source_meta_json, "
        "COALESCE(c.storage_tier, 'hot') AS storage_tier, "
        "bm25(fts_chunks) AS rank "
        "FROM fts_chunks "
        "JOIN chunks c ON c.rowid = fts_chunks.rowid "
        "JOIN sources s ON s.source_id = c.source_id "
        "WHERE fts_chunks MATCH ?"
    ]
    params: List[Any] = [_fts5_sanitize(query)]
    if role:
        sql.append("AND c.role=?")
        params.append(role)
    if kind:
        sql.append("AND s.kind=?")
        params.append(kind)
    if path_glob:
        sql.append("AND s.path GLOB ?")
        params.append(path_glob)
    if before is not None:
        sql.append("AND json_extract(c.meta_json, '$.create_time') <= ?")
        params.append(float(before))
    if after is not None:
        sql.append("AND json_extract(c.meta_json, '$.create_time') >= ?")
        params.append(float(after))
    sql.append("ORDER BY rank LIMIT ?")
    params.append(int(max(1, top_k)))

    rows = conn.execute(" ".join(sql), params).fetchall()
    hits: List[Hit] = []
    for r in rows:
        # bm25 returns lower-is-better; invert sign and clamp to a friendly 0..1-ish score.
        raw = float(r["rank"])
        score = 1.0 / (1.0 + max(0.0, raw))
        hits.append(
            Hit(
                chunk_id=r["chunk_id"],
                score=score,
                text=r["text"],
                role=r["role"],
                source_id=r["source_id"],
                path=r["path"],
                kind=r["kind"],
                mtime=r["mtime"],
                meta=json.loads(r["meta_json"] or "{}"),
                source_meta=json.loads(r["source_meta_json"] or "{}"),
                storage_tier=str(r["storage_tier"] or "hot"),
            )
        )
    return hits


def _fts5_sanitize(query: str) -> str:
    """Make a user-typed string safe for FTS5 MATCH, with BM25 keyword semantics.

    Two things matter:
    1. Strip punctuation per token. The tokenizer indexes alphanumeric terms, so a
       quoted token like "properties." or "0-dimensional" matches nothing.
    2. Join terms with OR, not whitespace. FTS5 treats space-separated terms as an
       implicit AND — a natural-language query then needs ONE document containing
       EVERY term verbatim, which matches ~nothing (this left the sparse lane dead).
       OR lets BM25 rank documents by term overlap, the conventional keyword search.
    """
    toks = re.findall(r"[a-z0-9]+", (query or "").lower())
    toks = [t for t in toks if len(t) >= 2]
    if not toks:
        return '""'
    return " OR ".join(f'"{t}"' for t in toks)


def list_conversations(
    conn: sqlite3.Connection,
    *,
    title_like: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    order: str = "newest",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Aggregate chunks by meta.conversation_id to list chat threads.

    `since`/`until` bound by the conversation's latest create_time.
    `order`: 'newest' | 'oldest' | 'most_messages'.
    """
    if order not in ("newest", "oldest", "most_messages"):
        raise ValueError(f"unknown order: {order!r}")

    sql = [
        "SELECT json_extract(c.meta_json, '$.conversation_id') AS conv_id, "
        "MAX(json_extract(c.meta_json, '$.conversation_title')) AS title, "
        "MIN(json_extract(c.meta_json, '$.create_time')) AS first_ts, "
        "MAX(json_extract(c.meta_json, '$.create_time')) AS last_ts, "
        "COUNT(*) AS msg_count "
        "FROM chunks c WHERE json_extract(c.meta_json, '$.conversation_id') IS NOT NULL"
    ]
    params: List[Any] = []
    if title_like:
        sql.append(
            "AND LOWER(COALESCE(json_extract(c.meta_json, '$.conversation_title'), '')) LIKE ?"
        )
        params.append(f"%{title_like.lower()}%")
    sql.append("GROUP BY conv_id")
    having: List[str] = []
    if since is not None:
        having.append("last_ts >= ?")
        params.append(float(since))
    if until is not None:
        having.append("last_ts <= ?")
        params.append(float(until))
    if having:
        sql.append("HAVING " + " AND ".join(having))
    if order == "newest":
        sql.append("ORDER BY last_ts DESC")
    elif order == "oldest":
        sql.append("ORDER BY first_ts ASC")
    else:
        sql.append("ORDER BY msg_count DESC")
    sql.append("LIMIT ?")
    params.append(int(max(1, limit)))

    rows = conn.execute(" ".join(sql), params).fetchall()
    return [
        {
            "conversation_id": r["conv_id"],
            "conversation_title": r["title"],
            "first_create_time": (float(r["first_ts"]) if r["first_ts"] is not None else None),
            "last_create_time": (float(r["last_ts"]) if r["last_ts"] is not None else None),
            "message_count": int(r["msg_count"]),
        }
        for r in rows
    ]


def get_conversation_chunks(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return chunks belonging to one conversation, ordered by create_time then seq."""
    rows = conn.execute(
        "SELECT c.chunk_id, c.source_id, c.seq, c.role, c.text, c.meta_json, "
        "s.path, s.kind, s.mtime "
        "FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        "WHERE json_extract(c.meta_json, '$.conversation_id') = ? "
        "ORDER BY json_extract(c.meta_json, '$.create_time') ASC, c.seq ASC "
        "LIMIT ?",
        (conversation_id, int(max(1, limit))),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        meta = json.loads(r["meta_json"] or "{}")
        out.append(
            {
                "chunk_id": r["chunk_id"],
                "source_id": r["source_id"],
                "seq": int(r["seq"]),
                "role": r["role"],
                "path": r["path"],
                "kind": r["kind"],
                "mtime": r["mtime"],
                "conversation_id": meta.get("conversation_id"),
                "conversation_title": meta.get("conversation_title"),
                "create_time": meta.get("create_time"),
                "text": r["text"],
            }
        )
    return out


def count_chunks(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
    return int(row["n"])


def count_sources(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()
    return int(row["n"])


def iter_source_ids(conn: sqlite3.Connection) -> Iterable[Tuple[str, str, str, float]]:
    """Yield (source_id, path, sha256, mtime) for all sources. Used by watcher reconciliation."""
    for row in conn.execute(
        "SELECT source_id, path, sha256, mtime FROM sources ORDER BY path"
    ):
        yield row["source_id"], row["path"], row["sha256"], float(row["mtime"])


# ---------------------------------------------------------------------------
# Identity graph + preference clusters
# ---------------------------------------------------------------------------


def _row_identity_claim(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "claim_id": row["claim_id"],
        "kind": row["kind"],
        "text": row["text"],
        "status": row["status"],
        "confidence": row["confidence"],
        "source_agent": row["source_agent"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "superseded_by": row["superseded_by"],
        "meta": json.loads(row["meta_json"] or "{}"),
    }


def identity_claim_insert(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    kind: str,
    text: str,
    status: str = "proposed",
    confidence: Optional[float] = None,
    source_agent: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO identity_claims(claim_id, kind, text, status, confidence, "
        "source_agent, created_at, updated_at, superseded_by, meta_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            claim_id,
            kind,
            text,
            status,
            confidence,
            source_agent,
            now,
            now,
            json.dumps(meta or {}, ensure_ascii=False),
        ),
    )


def identity_claim_get(conn: sqlite3.Connection, claim_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT claim_id, kind, text, status, confidence, source_agent, "
        "created_at, updated_at, superseded_by, meta_json "
        "FROM identity_claims WHERE claim_id=?",
        (claim_id,),
    ).fetchone()
    return _row_identity_claim(row) if row else None


def identity_claim_list(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    sql = (
        "SELECT claim_id, kind, text, status, confidence, source_agent, "
        "created_at, updated_at, superseded_by, meta_json "
        "FROM identity_claims WHERE 1=1"
    )
    params: List[Any] = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(int(max(1, min(limit, 2000))))
    return [_row_identity_claim(r) for r in conn.execute(sql, params).fetchall()]


def identity_claim_patch_fields(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    text: Optional[str] = None,
    status: Optional[str] = None,
    superseded_by: Optional[str] = None,
    meta_merge: Optional[Dict[str, Any]] = None,
) -> bool:
    row = conn.execute(
        "SELECT text, status, superseded_by, meta_json FROM identity_claims WHERE claim_id=?",
        (claim_id,),
    ).fetchone()
    if not row:
        return False
    new_text = text if text is not None else row["text"]
    new_status = status if status is not None else row["status"]
    new_sup = superseded_by if superseded_by is not None else row["superseded_by"]
    meta = json.loads(row["meta_json"] or "{}")
    if meta_merge:
        meta.update(meta_merge)
    now = time.time()
    conn.execute(
        "UPDATE identity_claims SET text=?, status=?, superseded_by=?, "
        "updated_at=?, meta_json=? WHERE claim_id=?",
        (
            new_text,
            new_status,
            new_sup,
            now,
            json.dumps(meta, ensure_ascii=False),
            claim_id,
        ),
    )
    return True


def identity_claim_set_status(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    status: str,
    superseded_by: Optional[str] = None,
) -> bool:
    return identity_claim_patch_fields(
        conn, claim_id, status=status, superseded_by=superseded_by
    )


def identity_edge_insert(
    conn: sqlite3.Connection,
    *,
    edge_id: str,
    claim_id: str,
    chunk_id: Optional[str] = None,
    source_id: Optional[str] = None,
    rationale: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO identity_edges(edge_id, claim_id, chunk_id, source_id, rationale, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (edge_id, claim_id, chunk_id, source_id, rationale, time.time()),
    )


def identity_edges_for_claim(conn: sqlite3.Connection, claim_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT edge_id, claim_id, chunk_id, source_id, rationale, created_at "
        "FROM identity_edges WHERE claim_id=? ORDER BY created_at",
        (claim_id,),
    ).fetchall()
    return [
        {
            "edge_id": r["edge_id"],
            "claim_id": r["claim_id"],
            "chunk_id": r["chunk_id"],
            "source_id": r["source_id"],
            "rationale": r["rationale"],
            "created_at": float(r["created_at"]),
        }
        for r in rows
    ]


def preference_clusters_clear(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM preference_clusters")


def preference_clusters_insert(
    conn: sqlite3.Connection,
    *,
    cluster_id: str,
    label: str,
    summary: str,
    member_chunk_ids: Sequence[str],
    run_at: float,
) -> None:
    conn.execute(
        "INSERT INTO preference_clusters(cluster_id, label, summary, member_chunk_ids_json, run_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (
            cluster_id,
            label,
            summary,
            json.dumps(list(member_chunk_ids), ensure_ascii=False),
            float(run_at),
        ),
    )


def preference_clusters_list(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT cluster_id, label, summary, member_chunk_ids_json, run_at "
        "FROM preference_clusters ORDER BY run_at DESC, cluster_id"
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "cluster_id": r["cluster_id"],
                "label": r["label"],
                "summary": r["summary"],
                "member_chunk_ids": json.loads(r["member_chunk_ids_json"] or "[]"),
                "run_at": float(r["run_at"]),
            }
        )
    return out


def identity_audit_log_append(
    conn: sqlite3.Connection,
    *,
    action: str,
    claim_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    conn.execute(
        "INSERT INTO identity_audit_log(ts, action, claim_id, detail_json) VALUES (?,?,?,?)",
        (
            time.time(),
            action.strip()[:128],
            claim_id,
            json.dumps(detail or {}, ensure_ascii=False),
        ),
    )


def identity_claim_mirror_history(
    conn: sqlite3.Connection, *, limit: int = 50
) -> List[Dict[str, Any]]:
    """Claims no longer active/proposed-only narrative — superseded or rejected."""
    lim = int(max(1, min(limit, 500)))
    rows = conn.execute(
        "SELECT claim_id, kind, text, status, confidence, source_agent, "
        "created_at, updated_at, superseded_by, meta_json FROM identity_claims "
        "WHERE status IN ('superseded', 'rejected') "
        "ORDER BY updated_at DESC LIMIT ?",
        (lim,),
    ).fetchall()
    return [_row_identity_claim(r) for r in rows]


def ambient_events_recent(
    conn: sqlite3.Connection, *, limit: int = 80
) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 2000)))
    rows = conn.execute(
        "SELECT event_id, event_type, captured_at, sensitivity, payload_json, storage_tier, dedupe_key "
        "FROM ambient_events ORDER BY captured_at DESC LIMIT ?",
        (lim,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "captured_at": float(r["captured_at"]),
                "sensitivity": r["sensitivity"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "storage_tier": r["storage_tier"],
                "dedupe_key": r["dedupe_key"],
            }
        )
    return out


def ambient_event_insert_ignore(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    captured_at: float,
    dedupe_key: str,
    payload: Dict[str, Any],
    sensitivity: str = "vault_local",
    storage_tier: str = "hot",
) -> bool:
    """Returns True if a row was inserted (dedupe_key unseen)."""
    event_id = "amb-" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:24]
    cur = conn.execute(
        "INSERT OR IGNORE INTO ambient_events(event_id, event_type, captured_at, sensitivity, "
        "payload_json, storage_tier, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            event_type.strip()[:64],
            float(captured_at),
            sensitivity.strip()[:64],
            json.dumps(payload, ensure_ascii=False),
            storage_tier.strip()[:32],
            dedupe_key[:2048],
        ),
    )
    return cur.rowcount == 1


def _row_screen_memory_event(r: sqlite3.Row) -> Dict[str, Any]:
    occurred_at = float(r["occurred_at"])
    events = json.loads(r["actions_json"] or "[]")
    raw = json.loads(r["raw_json"] or "{}")
    return {
        "event_id": r["event_id"],
        "occurred_at": occurred_at,
        "time": datetime.fromtimestamp(occurred_at, timezone.utc).isoformat().replace("+00:00", "Z"),
        "app": r["app"],
        "window": r["window"],
        "url": r["url"],
        "scene": r["scene"],
        "visible_elements": json.loads(r["visible_elements_json"] or "[]"),
        "events": events,
        "source_refs": json.loads(r["source_refs_json"] or "[]"),
        "confidence": float(r["confidence"] or 0.0),
        "trust_tier": r["trust_tier"],
        "time_range": _screen_event_time_range_from_row(events, raw),
        "clip_path": _screen_event_clip_path_from_row(raw),
        "raw": raw,
        "created_at": float(r["created_at"]),
        "updated_at": float(r["updated_at"]),
    }


def _screen_event_time_range_from_row(events: List[Dict[str, Any]], raw: Dict[str, Any]) -> str:
    for action in events:
        if isinstance(action, dict) and action.get("time_range"):
            return str(action.get("time_range") or "").strip()
    payload = raw.get("payload") if isinstance(raw, dict) else {}
    if isinstance(payload, dict):
        return str(payload.get("time_range") or "").strip()
    return ""


def _screen_event_clip_path_from_row(raw: Dict[str, Any]) -> str:
    payload = raw.get("payload") if isinstance(raw, dict) else {}
    if isinstance(payload, dict):
        return str(payload.get("clip_path") or payload.get("source_path") or "").strip()
    return ""


def screen_memory_event_upsert(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    occurred_at: float,
    app: str = "",
    window: str = "",
    url: Optional[str] = None,
    scene: str = "",
    visible_elements: Optional[List[Dict[str, Any]]] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    source_refs: Optional[List[str]] = None,
    confidence: float = 0.0,
    trust_tier: str = "unknown",
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO screen_memory_events(event_id, occurred_at, app, window, url, scene, "
        "visible_elements_json, actions_json, source_refs_json, confidence, trust_tier, "
        "raw_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(event_id) DO UPDATE SET occurred_at=excluded.occurred_at, "
        "app=excluded.app, window=excluded.window, url=excluded.url, scene=excluded.scene, "
        "visible_elements_json=excluded.visible_elements_json, actions_json=excluded.actions_json, "
        "source_refs_json=excluded.source_refs_json, confidence=excluded.confidence, "
        "trust_tier=excluded.trust_tier, raw_json=excluded.raw_json, updated_at=excluded.updated_at",
        (
            event_id,
            float(occurred_at),
            app.strip()[:200],
            window.strip()[:500],
            url,
            scene.strip()[:1000],
            json.dumps(visible_elements or [], ensure_ascii=False),
            json.dumps(events or [], ensure_ascii=False),
            json.dumps(source_refs or [], ensure_ascii=False),
            float(max(0.0, min(confidence, 1.0))),
            trust_tier.strip()[:64],
            json.dumps(raw or {}, ensure_ascii=False),
            now,
            now,
        ),
    )


def screen_memory_events_since(
    conn: sqlite3.Connection,
    *,
    since_ts: float = 0.0,
    limit: int = 100,
    app: Optional[str] = None,
) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 1000)))
    sql = [
        "SELECT event_id, occurred_at, app, window, url, scene, visible_elements_json, "
        "actions_json, source_refs_json, confidence, trust_tier, raw_json, created_at, updated_at "
        "FROM screen_memory_events WHERE occurred_at >= ?"
    ]
    params: List[Any] = [float(since_ts)]
    if app:
        sql.append("AND app = ?")
        params.append(app)
    sql.append("ORDER BY occurred_at DESC LIMIT ?")
    params.append(lim)
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [_row_screen_memory_event(r) for r in rows]


def _row_graph_candidate(r: sqlite3.Row) -> Dict[str, Any]:
    return {
        "candidate_id": r["candidate_id"],
        "candidate_type": r["candidate_type"],
        "status": r["status"],
        "title": r["title"],
        "body_md": r["body_md"],
        "payload": json.loads(r["payload_json"] or "{}"),
        "evidence_refs": json.loads(r["evidence_refs_json"] or "[]"),
        "confidence": float(r["confidence"] or 0),
        "source": r["source"],
        "created_at": float(r["created_at"]),
        "updated_at": float(r["updated_at"]),
        "resolved_at": float(r["resolved_at"]) if r["resolved_at"] is not None else None,
    }


def graph_candidate_create(
    conn: sqlite3.Connection,
    *,
    candidate_type: str,
    title: str,
    body_md: str = "",
    payload: Optional[Dict[str, Any]] = None,
    evidence_refs: Optional[List[str]] = None,
    confidence: float = 0.0,
    source: str = "graph",
) -> str:
    now = time.time()
    cid = _new_id("gc")
    conn.execute(
        "INSERT INTO graph_candidates(candidate_id, candidate_type, status, title, body_md, "
        "payload_json, evidence_refs_json, confidence, source, created_at, updated_at) "
        "VALUES(?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cid,
            candidate_type.strip()[:64],
            title.strip()[:300],
            body_md,
            json.dumps(payload or {}, ensure_ascii=False),
            json.dumps(evidence_refs or [], ensure_ascii=False),
            float(confidence),
            source.strip()[:64],
            now,
            now,
        ),
    )
    return cid


def graph_candidate_list(
    conn: sqlite3.Connection,
    *,
    status: str = "open",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 200)))
    rows = conn.execute(
        "SELECT candidate_id, candidate_type, status, title, body_md, payload_json, "
        "evidence_refs_json, confidence, source, created_at, updated_at, resolved_at "
        "FROM graph_candidates WHERE status=? ORDER BY updated_at DESC LIMIT ?",
        (status.strip()[:32], lim),
    ).fetchall()
    return [_row_graph_candidate(r) for r in rows]


def graph_candidate_get(
    conn: sqlite3.Connection, candidate_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT candidate_id, candidate_type, status, title, body_md, payload_json, "
        "evidence_refs_json, confidence, source, created_at, updated_at, resolved_at "
        "FROM graph_candidates WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    return _row_graph_candidate(row) if row else None


def graph_candidate_resolve(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    status: str,
    payload_merge: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    current = graph_candidate_get(conn, candidate_id)
    if not current:
        return None
    payload = dict(current.get("payload") or {})
    if payload_merge:
        payload.update(payload_merge)
    now = time.time()
    conn.execute(
        "UPDATE graph_candidates SET status=?, payload_json=?, updated_at=?, resolved_at=? "
        "WHERE candidate_id=?",
        (
            status.strip()[:32],
            json.dumps(payload, ensure_ascii=False),
            now,
            now,
            candidate_id,
        ),
    )
    return graph_candidate_get(conn, candidate_id)


def chunk_storage_tier_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT COALESCE(storage_tier, 'hot') AS t, COUNT(*) AS n FROM chunks GROUP BY t"
    ).fetchall()
    return {str(r["t"]): int(r["n"]) for r in rows}


def sqlite_storage_fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Cheap PRAGMA snapshot for maintenance UI (free list → reclaimable space after offline VACUUM)."""
    pc_row = conn.execute("PRAGMA page_count").fetchone()
    fc_row = conn.execute("PRAGMA freelist_count").fetchone()
    ps_row = conn.execute("PRAGMA page_size").fetchone()
    page_count = int(pc_row[0]) if pc_row and pc_row[0] is not None else 0
    freelist_pages = int(fc_row[0]) if fc_row and fc_row[0] is not None else 0
    page_size = int(ps_row[0]) if ps_row and ps_row[0] is not None else 0
    logical_bytes = page_count * page_size
    freelist_bytes_approx = freelist_pages * page_size
    freelist_ratio = (freelist_pages / page_count) if page_count > 0 else 0.0

    db_path_str = ""
    disk_bytes: Optional[int] = None
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main" and row["file"]:
            db_path_str = str(row["file"])
            p = Path(db_path_str)
            if p.is_file():
                try:
                    disk_bytes = int(p.stat().st_size)
                except OSError:
                    disk_bytes = None
            break

    return {
        "page_count": page_count,
        "freelist_pages": freelist_pages,
        "page_size": page_size,
        "logical_bytes": logical_bytes,
        "freelist_bytes_approx": freelist_bytes_approx,
        "freelist_ratio": round(freelist_ratio, 8),
        "db_file_bytes": disk_bytes,
        "db_path": db_path_str or None,
    }


_STORAGE_TIER_RANK: Dict[str, int] = {"hot": 0, "warm": 1, "cold": 2}


def normalize_storage_tier_name(name: str) -> str:
    t = (name or "hot").strip().lower()
    return t if t in _STORAGE_TIER_RANK else "hot"


def validate_stale_tier_promotion(from_tier: str, to_tier: str) -> Tuple[str, str]:
    """``from`` must be strictly fresher than ``to`` (hot→warm→cold). Returns normalized names."""
    f = normalize_storage_tier_name(from_tier)
    t = normalize_storage_tier_name(to_tier)
    if _STORAGE_TIER_RANK[f] >= _STORAGE_TIER_RANK[t]:
        raise ValueError(
            "stale-source tier promotion requires from_tier strictly fresher than to_tier "
            f"(e.g. hot→warm); got {f!r} -> {t!r}"
        )
    return f, t


def count_chunks_stale_source_tier_promotion_candidates(
    conn: sqlite3.Connection,
    *,
    source_updated_before: float,
    source_kinds: Optional[Sequence[str]] = None,
    from_tier: str = "hot",
) -> int:
    """Count chunks currently at ``from_tier`` whose parent sources are older than threshold."""
    ft = normalize_storage_tier_name(from_tier)
    kinds_norm: Optional[List[str]] = None
    if source_kinds:
        kinds_norm = [k.strip().lower() for k in source_kinds if k and k.strip()]
        if not kinds_norm:
            kinds_norm = None
    base = """
            SELECT COUNT(*) AS n FROM chunks c
            JOIN sources s ON s.source_id = c.source_id
            WHERE COALESCE(c.storage_tier, 'hot') = ? AND s.updated_at < ?
    """
    if kinds_norm:
        placeholders = ",".join("?" * len(kinds_norm))
        row = conn.execute(
            f"{base} AND lower(s.kind) IN ({placeholders})",
            (ft, float(source_updated_before), *kinds_norm),
        ).fetchone()
    else:
        row = conn.execute(
            base,
            (ft, float(source_updated_before)),
        ).fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0


def promote_chunks_for_stale_sources(
    conn: sqlite3.Connection,
    *,
    source_updated_before: float,
    source_kinds: Optional[Sequence[str]] = None,
    from_tier: str,
    to_tier: str,
) -> int:
    """Bump ``storage_tier`` from ``from_tier`` to ``to_tier`` when sources are stale. Returns rows changed."""
    f_norm, t_norm = validate_stale_tier_promotion(from_tier, to_tier)
    kinds_norm: Optional[List[str]] = None
    if source_kinds:
        kinds_norm = [k.strip().lower() for k in source_kinds if k and k.strip()]
        if not kinds_norm:
            kinds_norm = None
    before = int(conn.total_changes)
    sub = [
        "SELECT c.chunk_id FROM chunks c ",
        "JOIN sources s ON s.source_id = c.source_id ",
        "WHERE COALESCE(c.storage_tier, 'hot') = ? AND s.updated_at < ? ",
    ]
    params: List[Any] = [f_norm, float(source_updated_before)]
    if kinds_norm:
        placeholders = ",".join("?" * len(kinds_norm))
        sub.append(f"AND lower(s.kind) IN ({placeholders})")
        params.extend(kinds_norm)
    sql_sub = "".join(sub)
    conn.execute(
        f"UPDATE chunks SET storage_tier = ? WHERE chunk_id IN ({sql_sub})",
        [t_norm, *params],
    )
    return int(conn.total_changes) - before


def count_chunks_hot_to_warm_candidates(
    conn: sqlite3.Connection,
    *,
    source_updated_before: float,
    source_kinds: Optional[Sequence[str]] = None,
) -> int:
    """Compatibility: ``hot`` rows eligible for promotion to ``warm``."""
    return count_chunks_stale_source_tier_promotion_candidates(
        conn,
        source_updated_before=source_updated_before,
        source_kinds=source_kinds,
        from_tier="hot",
    )


def promote_chunks_hot_to_warm_for_stale_sources(
    conn: sqlite3.Connection,
    *,
    source_updated_before: float,
    source_kinds: Optional[Sequence[str]] = None,
) -> int:
    """Compatibility: ``hot`` → ``warm`` for stale sources."""
    return promote_chunks_for_stale_sources(
        conn,
        source_updated_before=source_updated_before,
        source_kinds=source_kinds,
        from_tier="hot",
        to_tier="warm",
    )


def iter_chunk_embedding_rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
) -> List[Tuple[str, str, np.ndarray]]:
    """Return (chunk_id, text, embedding_vector) for up to `limit` chunks with vectors."""
    rows = conn.execute(
        "SELECT c.chunk_id, c.text, v.embedding AS emb "
        "FROM chunks c JOIN vec_chunks v ON v.rowid = c.rowid "
        "ORDER BY RANDOM() LIMIT ?",
        (int(max(1, limit)),),
    ).fetchall()
    out: List[Tuple[str, str, np.ndarray]] = []
    dim = get_embed_dim(conn)
    for r in rows:
        blob = r["emb"]
        if blob is None:
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        if vec.size != dim:
            continue
        out.append((str(r["chunk_id"]), str(r["text"]), vec.copy()))
    return out


# ---------------------------------------------------------------------------
# Second-brain entities (wiki, tasks, outputs, sync health)
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}-{hashlib.sha256(f'{prefix}{time.time()}{os.urandom(8)!r}'.encode()).hexdigest()[:20]}"


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def seed_sync_sources(conn: sqlite3.Connection) -> None:
    defaults = (
        ("inbox", "Inbox watcher"),
        ("screen_context", "macOS screen context"),
        ("ambient_ax_index", "Accessibility text indexer"),
        ("ambient_fs", "Home filesystem watcher"),
        ("ambient_process", "Process snapshot collector"),
        ("ambient_listening", "Mic listening sessions"),
    )
    for key, label in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO sync_sources(source_key, label, status, config_json) "
            "VALUES(?, ?, 'healthy', '{}')",
            (key, label),
        )


def sync_source_touch(
    conn: sqlite3.Connection,
    source_key: str,
    *,
    status: str = "healthy",
    items: int = 0,
) -> None:
    seed_sync_sources(conn)
    now = time.time()
    conn.execute(
        "UPDATE sync_sources SET status=?, last_sync_at=? WHERE source_key=?",
        (status.strip()[:32], now, source_key),
    )


def sync_sources_list(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT source_key, label, status, last_sync_at, config_json FROM sync_sources ORDER BY source_key"
    ).fetchall()
    return [
        {
            "source_key": r["source_key"],
            "label": r["label"],
            "status": r["status"],
            "last_sync_at": float(r["last_sync_at"]) if r["last_sync_at"] is not None else None,
            "config": json.loads(r["config_json"] or "{}"),
        }
        for r in rows
    ]


def sync_job_run_append(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    status: str,
    started_at: float,
    finished_at: Optional[float],
    items_count: int = 0,
    error: Optional[str] = None,
) -> str:
    run_id = _new_id("run")
    conn.execute(
        "INSERT INTO sync_job_runs(run_id, source_key, status, started_at, finished_at, items_count, error) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            source_key,
            status[:32],
            float(started_at),
            float(finished_at) if finished_at is not None else None,
            int(items_count),
            (error or "")[:2000] or None,
        ),
    )
    return run_id


def system_issue_upsert(
    conn: sqlite3.Connection,
    *,
    issue_id: str,
    severity: str,
    source_key: Optional[str],
    body_md: str,
) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO system_issues(issue_id, severity, source_key, body_md, status, created_at) "
        "VALUES(?, ?, ?, ?, 'open', ?) "
        "ON CONFLICT(issue_id) DO UPDATE SET "
        "severity=excluded.severity, body_md=excluded.body_md, status='open', resolved_at=NULL",
        (issue_id, severity[:32], source_key, body_md, now),
    )


def system_issues_open(conn: sqlite3.Connection, *, limit: int = 50) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 200)))
    rows = conn.execute(
        "SELECT issue_id, severity, source_key, body_md, status, created_at, resolved_at "
        "FROM system_issues WHERE status='open' ORDER BY created_at DESC LIMIT ?",
        (lim,),
    ).fetchall()
    return [
        {
            "issue_id": r["issue_id"],
            "severity": r["severity"],
            "source_key": r["source_key"],
            "body_md": r["body_md"],
            "status": r["status"],
            "created_at": float(r["created_at"]),
            "resolved_at": float(r["resolved_at"]) if r["resolved_at"] is not None else None,
        }
        for r in rows
    ]


def system_issue_resolve(conn: sqlite3.Connection, issue_id: str) -> bool:
    cur = conn.execute(
        "UPDATE system_issues SET status='resolved', resolved_at=? WHERE issue_id=? AND status='open'",
        (time.time(), issue_id),
    )
    return cur.rowcount > 0


def _row_wiki_page(r: sqlite3.Row) -> Dict[str, Any]:
    return {
        "page_id": r["page_id"],
        "page_type": r["page_type"],
        "title": r["title"],
        "body_md": r["body_md"],
        "status": r["status"],
        "last_updated": float(r["last_updated"]),
        "meta": json.loads(r["meta_json"] or "{}"),
        "storage_tier": r["storage_tier"],
    }


def wiki_page_list(
    conn: sqlite3.Connection,
    *,
    page_type: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 500)))
    clauses: List[str] = []
    params: List[Any] = []
    if page_type:
        clauses.append("page_type=?")
        params.append(page_type.strip()[:64])
    if status:
        clauses.append("status=?")
        params.append(status.strip()[:32])
    if q:
        clauses.append("(title LIKE ? OR body_md LIKE ?)")
        like = f"%{q.strip()[:200]}%"
        params.extend([like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT page_id, page_type, title, body_md, status, last_updated, meta_json, storage_tier "
        f"FROM wiki_pages{where} ORDER BY last_updated DESC LIMIT ?",
        (*params, lim),
    ).fetchall()
    return [_row_wiki_page(r) for r in rows]


def wiki_page_get(conn: sqlite3.Connection, page_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT page_id, page_type, title, body_md, status, last_updated, meta_json, storage_tier "
        "FROM wiki_pages WHERE page_id=?",
        (page_id,),
    ).fetchone()
    return _row_wiki_page(row) if row else None


def wiki_page_upsert(
    conn: sqlite3.Connection,
    *,
    page_id: Optional[str],
    page_type: str,
    title: str,
    body_md: str,
    status: str = "active",
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    now = time.time()
    pid = page_id or _new_id("wiki")
    conn.execute(
        "INSERT INTO wiki_pages(page_id, page_type, title, body_md, status, last_updated, meta_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(page_id) DO UPDATE SET "
        "page_type=excluded.page_type, title=excluded.title, body_md=excluded.body_md, "
        "status=excluded.status, last_updated=excluded.last_updated, meta_json=excluded.meta_json",
        (
            pid,
            page_type.strip()[:64],
            title.strip()[:500],
            body_md,
            status.strip()[:32],
            now,
            json.dumps(meta or {}, ensure_ascii=False),
        ),
    )
    return pid


def wiki_page_delete(conn: sqlite3.Connection, page_id: str) -> bool:
    cur = conn.execute("DELETE FROM wiki_pages WHERE page_id=?", (page_id,))
    return cur.rowcount > 0


def wiki_link_add(
    conn: sqlite3.Connection,
    *,
    from_page_id: str,
    to_page_id: str,
    link_kind: str = "related",
) -> str:
    lid = _new_id("lnk")
    conn.execute(
        "INSERT INTO wiki_links(link_id, from_page_id, to_page_id, link_kind, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (lid, from_page_id, to_page_id, link_kind[:32], time.time()),
    )
    return lid


def wiki_links_for_page(conn: sqlite3.Connection, page_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT l.link_id, l.from_page_id, l.to_page_id, l.link_kind, l.created_at, "
        "p.title AS to_title, p.page_type AS to_type "
        "FROM wiki_links l JOIN wiki_pages p ON p.page_id = l.to_page_id "
        "WHERE l.from_page_id=? ORDER BY l.created_at DESC",
        (page_id,),
    ).fetchall()
    return [
        {
            "link_id": r["link_id"],
            "from_page_id": r["from_page_id"],
            "to_page_id": r["to_page_id"],
            "link_kind": r["link_kind"],
            "created_at": float(r["created_at"]),
            "to_title": r["to_title"],
            "to_type": r["to_type"],
        }
        for r in rows
    ]


def _row_task(r: sqlite3.Row) -> Dict[str, Any]:
    return {
        "task_id": r["task_id"],
        "title": r["title"],
        "status": r["status"],
        "origin": r["origin"],
        "priority": r["priority"],
        "due_at": float(r["due_at"]) if r["due_at"] is not None else None,
        "body_md": r["body_md"],
        "context_refs": json.loads(r["context_refs_json"] or "[]"),
        "wiki_refs": json.loads(r["wiki_refs_json"] or "[]"),
        "output_id": r["output_id"],
        "created_at": float(r["created_at"]),
        "updated_at": float(r["updated_at"]),
    }


def task_list(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    origin: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 500)))
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status.strip()[:32])
    if origin:
        clauses.append("origin=?")
        params.append(origin.strip()[:32])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT task_id, title, status, origin, priority, due_at, body_md, context_refs_json, "
        f"wiki_refs_json, output_id, created_at, updated_at FROM tasks{where} "
        f"ORDER BY updated_at DESC LIMIT ?",
        (*params, lim),
    ).fetchall()
    return [_row_task(r) for r in rows]


def task_get(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT task_id, title, status, origin, priority, due_at, body_md, context_refs_json, "
        "wiki_refs_json, output_id, created_at, updated_at FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    return _row_task(row) if row else None


def task_infer_insert(
    conn: sqlite3.Connection,
    *,
    title: str,
    body_md: str = "",
    origin: str = "inferred",
    priority: Optional[str] = None,
    context_refs: Optional[List[Any]] = None,
    wiki_refs: Optional[List[str]] = None,
) -> str:
    now = time.time()
    tid = _new_id("task")
    conn.execute(
        "INSERT INTO tasks(task_id, title, status, origin, priority, body_md, context_refs_json, "
        "wiki_refs_json, created_at, updated_at) VALUES(?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
        (
            tid,
            title.strip()[:500],
            origin.strip()[:32],
            priority,
            body_md,
            json.dumps(context_refs or [], ensure_ascii=False),
            json.dumps(wiki_refs or [], ensure_ascii=False),
            now,
            now,
        ),
    )
    return tid


def task_patch(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: Optional[str] = None,
    title: Optional[str] = None,
    body_md: Optional[str] = None,
    output_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    fields: List[str] = []
    params: List[Any] = []
    if status is not None:
        fields.append("status=?")
        params.append(status.strip()[:32])
    if title is not None:
        fields.append("title=?")
        params.append(title.strip()[:500])
    if body_md is not None:
        fields.append("body_md=?")
        params.append(body_md)
    if output_id is not None:
        fields.append("output_id=?")
        params.append(output_id)
    if not fields:
        return task_get(conn, task_id)
    fields.append("updated_at=?")
    params.append(time.time())
    params.append(task_id)
    cur = conn.execute(
        f"UPDATE tasks SET {', '.join(fields)} WHERE task_id=?",
        params,
    )
    if cur.rowcount == 0:
        return None
    return task_get(conn, task_id)


def output_create(
    conn: sqlite3.Connection,
    *,
    task_id: Optional[str],
    kind: str,
    body_md: str,
    wiki_read: Optional[List[str]] = None,
) -> str:
    now = time.time()
    oid = _new_id("out")
    conn.execute(
        "INSERT INTO outputs(output_id, task_id, kind, body_md, status, wiki_read_json, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, 'draft', ?, ?, ?)",
        (
            oid,
            task_id,
            kind.strip()[:64],
            body_md,
            json.dumps(wiki_read or [], ensure_ascii=False),
            now,
            now,
        ),
    )
    if task_id:
        task_patch(conn, task_id, output_id=oid, status="review")
    return oid


def output_get(conn: sqlite3.Connection, output_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT output_id, task_id, kind, body_md, status, wiki_read_json, created_at, updated_at "
        "FROM outputs WHERE output_id=?",
        (output_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "output_id": row["output_id"],
        "task_id": row["task_id"],
        "kind": row["kind"],
        "body_md": row["body_md"],
        "status": row["status"],
        "wiki_read": json.loads(row["wiki_read_json"] or "[]"),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def output_patch(
    conn: sqlite3.Connection,
    output_id: str,
    *,
    status: Optional[str] = None,
    body_md: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    fields: List[str] = []
    params: List[Any] = []
    if status is not None:
        fields.append("status=?")
        params.append(status.strip()[:32])
    if body_md is not None:
        fields.append("body_md=?")
        params.append(body_md)
    if not fields:
        return output_get(conn, output_id)
    fields.append("updated_at=?")
    params.append(time.time())
    params.append(output_id)
    cur = conn.execute(
        f"UPDATE outputs SET {', '.join(fields)} WHERE output_id=?",
        params,
    )
    if cur.rowcount == 0:
        return None
    return output_get(conn, output_id)


def ambient_events_since(
    conn: sqlite3.Connection,
    *,
    since_ts: float,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 2000)))
    rows = conn.execute(
        "SELECT event_id, event_type, captured_at, sensitivity, payload_json, storage_tier, dedupe_key "
        "FROM ambient_events WHERE captured_at >= ? ORDER BY captured_at DESC LIMIT ?",
        (float(since_ts), lim),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "captured_at": float(r["captured_at"]),
                "sensitivity": r["sensitivity"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "storage_tier": r["storage_tier"],
                "dedupe_key": r["dedupe_key"],
            }
        )
    return out


GRAPH_KINDS: tuple[tuple[str, str, str], ...] = ()  # legacy flat kinds; see life_graph.py

WIKI_PAGE_TYPE_TO_GRAPH: Dict[str, str] = {}


def _import_life_graph():
    from life_graph import LIFE_GRAPH_SCAFFOLD, WIKI_PAGE_TYPE_TO_NODE

    return LIFE_GRAPH_SCAFFOLD, WIKI_PAGE_TYPE_TO_NODE


def seed_graph_scaffold(conn: sqlite3.Connection) -> None:
    """Seed the pre-generated life graph tree (see docs/LIFE_GRAPH.md)."""
    scaffold, wiki_map = _import_life_graph()
    global WIKI_PAGE_TYPE_TO_GRAPH
    WIKI_PAGE_TYPE_TO_GRAPH = wiki_map
    now = time.time()
    for node_id, parent_id, node_type, title, hint in scaffold:
        conn.execute(
            "INSERT OR IGNORE INTO graph_nodes("
            "node_id, node_kind, title, status, body_md, wiki_page_id, "
            "parent_node_id, aliases_json, summary, confidence, source_refs_json, "
            "privacy_level, created_at, updated_at"
            ") VALUES(?, ?, ?, 'scaffold', ?, NULL, ?, '[]', ?, 0.0, '[]', 'vault_local', ?, ?)",
            (node_id, node_type, title, hint, parent_id, hint, now, now),
        )


def _row_graph_node(r: sqlite3.Row) -> Dict[str, Any]:
    return {
        "node_id": r["node_id"],
        "node_kind": r["node_kind"],
        "title": r["title"],
        "status": r["status"],
        "body_md": r["body_md"],
        "wiki_page_id": r["wiki_page_id"],
        "parent_node_id": r["parent_node_id"] if "parent_node_id" in r.keys() else None,
        "aliases": json.loads(r["aliases_json"] or "[]") if "aliases_json" in r.keys() else [],
        "summary": (r["summary"] or "") if "summary" in r.keys() else "",
        "confidence": float(r["confidence"] or 0) if "confidence" in r.keys() else 0.0,
        "source_refs": json.loads(r["source_refs_json"] or "[]")
        if "source_refs_json" in r.keys()
        else [],
        "privacy_level": (r["privacy_level"] or "vault_local")
        if "privacy_level" in r.keys()
        else "vault_local",
        "created_at": float(r["created_at"]),
        "updated_at": float(r["updated_at"]),
    }


def graph_scaffold_nodes(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    scaffold, _ = _import_life_graph()
    order = {spec[0]: i for i, spec in enumerate(scaffold)}
    rows = conn.execute(
        "SELECT node_id, node_kind, title, status, body_md, wiki_page_id, parent_node_id, "
        "aliases_json, summary, confidence, source_refs_json, privacy_level, created_at, updated_at "
        "FROM graph_nodes WHERE status='scaffold'"
    ).fetchall()
    nodes = [_row_graph_node(r) for r in rows]
    nodes.sort(key=lambda n: order.get(n["node_id"], 999))
    return nodes


def graph_filled_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Count non-scaffold nodes + wiki pages mapped by type."""
    _, wiki_map = _import_life_graph()
    out: Dict[str, int] = {}
    rows = conn.execute(
        "SELECT node_kind, COUNT(*) AS n FROM graph_nodes "
        "WHERE status NOT IN ('stub', 'scaffold') GROUP BY node_kind"
    ).fetchall()
    for r in rows:
        out[str(r["node_kind"])] = int(r["n"])
    wiki_rows = conn.execute(
        "SELECT page_type, COUNT(*) AS n FROM wiki_pages WHERE status='active' GROUP BY page_type"
    ).fetchall()
    for r in wiki_rows:
        nk = wiki_map.get(str(r["page_type"] or ""))
        if nk:
            out[nk] = out.get(nk, 0) + int(r["n"])
    return out


def _graph_summary_snippet(summary: Any) -> str:
    if not summary:
        return ""
    raw = str(summary).strip()
    if raw.startswith("{"):
        try:
            meta = json.loads(raw)
            note = str(meta.get("user_note") or meta.get("relation_note") or "").strip()
            if note:
                return note[:100]
        except json.JSONDecodeError:
            pass
    return raw[:100]


def graph_members_by_parent(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    """User-filled nodes keyed by scaffold parent_node_id."""
    scaffold, wiki_map = _import_life_graph()
    default_parent_by_kind = {
        "person": "scaffold-people-unknown",
        "family": "scaffold-people-family",
        "place": "scaffold-places-frequent",
        "group": "scaffold-groups-teams",
        "organization": "scaffold-groups-companies",
        "project": "scaffold-projects-active",
        "role": "scaffold-work-roles",
        "job": "scaffold-work-roles",
        "hobby": "scaffold-hobbies",
        "asset": "scaffold-assets",
        "task": "scaffold-tasks",
        "decision": "scaffold-decisions",
        "preference": "scaffold-preferences",
        "obligation": "scaffold-work-obligations",
    }
    scaffold_ids = {spec[0] for spec in scaffold}
    rows = conn.execute(
        "SELECT node_id, node_kind, title, status, parent_node_id, summary, updated_at "
        "FROM graph_nodes WHERE status NOT IN ('scaffold', 'stub') "
        "ORDER BY updated_at DESC"
    ).fetchall()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        pid = str(r["parent_node_id"] or "")
        out.setdefault(pid, []).append(
            {
                "node_id": str(r["node_id"]),
                "node_kind": str(r["node_kind"]),
                "title": str(r["title"] or ""),
                "summary_snippet": _graph_summary_snippet(r["summary"]),
            }
        )
    wiki_rows = conn.execute(
        "SELECT page_id, page_type, title, body_md, last_updated FROM wiki_pages w "
        "WHERE status='active' AND NOT EXISTS("
        "SELECT 1 FROM graph_nodes n WHERE n.wiki_page_id=w.page_id)"
        "ORDER BY last_updated DESC"
    ).fetchall()
    for r in wiki_rows:
        kind = wiki_map.get(str(r["page_type"] or ""))
        parent = default_parent_by_kind.get(str(kind or ""))
        if not parent or parent not in scaffold_ids:
            continue
        out.setdefault(parent, []).append(
            {
                "node_id": f"wiki:{r['page_id']}",
                "node_kind": str(kind),
                "title": str(r["title"] or ""),
                "summary_snippet": _graph_summary_snippet(r["body_md"]),
            }
        )
    return out


def graph_scaffold_list(conn: sqlite3.Connection) -> Dict[str, Any]:
    from life_graph import NODE_TYPES, RELATION_TYPES, scaffold_as_tree

    nodes = graph_scaffold_nodes(conn)
    members_by_parent = graph_members_by_parent(conn)

    def _bucket_ids(node_id: str) -> List[str]:
        ids = [node_id]
        for n in nodes:
            if n.get("parent_node_id") == node_id:
                ids.extend(_bucket_ids(n["node_id"]))
        return ids

    def _count_under(node_id: str) -> int:
        total = 0
        for bid in _bucket_ids(node_id):
            total += len(members_by_parent.get(bid, []))
        return total

    def _attach_counts(node: Dict[str, Any]) -> Dict[str, Any]:
        direct = members_by_parent.get(node["node_id"], [])
        children = [_attach_counts(c) for c in node.get("children") or []]
        if str(node.get("node_kind")) == "scaffold":
            filled = _count_under(node["node_id"])
        else:
            filled = len(direct)
        return {
            **node,
            "filled_count": filled,
            "members": direct[:12],
            "children": children,
        }

    flat = [{**n, "children": []} for n in nodes]
    tree = scaffold_as_tree(flat)
    tree = [_attach_counts(n) for n in tree]

    totals = graph_filled_counts(conn)
    highlights: List[Dict[str, Any]] = []
    title_by_id = {n["node_id"]: n["title"] for n in nodes}
    for r in conn.execute(
        "SELECT node_id, node_kind, title, parent_node_id, summary "
        "FROM graph_nodes WHERE status NOT IN ('scaffold', 'stub') "
        "ORDER BY updated_at DESC LIMIT 24"
    ).fetchall():
        pid = str(r["parent_node_id"] or "")
        highlights.append(
            {
                "node_id": str(r["node_id"]),
                "node_kind": str(r["node_kind"]),
                "title": str(r["title"] or ""),
                "bucket": title_by_id.get(pid, ""),
                "summary_snippet": _graph_summary_snippet(r["summary"]),
            }
        )

    kinds = [
        {
            "kind": n["node_kind"],
            "label": n["title"],
            "hint": n.get("summary") or n.get("body_md") or "",
            "stub_node_id": n["node_id"],
            "filled_count": _count_under(n["node_id"])
            if n["node_kind"] == "scaffold"
            else len(members_by_parent.get(n["node_id"], [])),
        }
        for n in flat
        if n["node_id"] != "scaffold-me"
    ]

    return {
        "root": tree[0] if tree else None,
        "tree": tree,
        "kinds": kinds,
        "node_types": list(NODE_TYPES),
        "relation_types": list(RELATION_TYPES),
        "totals": totals,
        "highlights": highlights,
        "user_node_count": sum(totals.values()),
    }


def sync_job_runs_recent(
    conn: sqlite3.Connection, *, limit: int = 40
) -> List[Dict[str, Any]]:
    lim = int(max(1, min(limit, 500)))
    rows = conn.execute(
        "SELECT run_id, source_key, status, started_at, finished_at, items_count, error "
        "FROM sync_job_runs ORDER BY started_at DESC LIMIT ?",
        (lim,),
    ).fetchall()
    return [
        {
            "run_id": r["run_id"],
            "source_key": r["source_key"],
            "status": r["status"],
            "started_at": float(r["started_at"]),
            "finished_at": float(r["finished_at"]) if r["finished_at"] is not None else None,
            "items_count": int(r["items_count"]),
            "error": r["error"],
        }
        for r in rows
    ]
