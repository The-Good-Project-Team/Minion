"""Chat threads for graph clarification."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from store import _new_id


def _migrate_chat_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_threads (
            thread_id TEXT PRIMARY KEY,
            subject_id TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            topic TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            body_md TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages(thread_id, created_at)"
    )


def chat_thread_insert(
    conn,
    *,
    subject_id: Optional[str] = None,
    topic: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    tid = _new_id("cth")
    now = time.time()
    conn.execute(
        "INSERT INTO chat_threads(thread_id, subject_id, status, topic, meta_json, created_at, updated_at) "
        "VALUES(?, ?, 'open', ?, ?, ?, ?)",
        (
            tid,
            subject_id,
            topic[:200],
            json.dumps(meta or {}, ensure_ascii=False),
            now,
            now,
        ),
    )
    return tid


def chat_message_insert(
    conn,
    *,
    thread_id: str,
    role: str,
    body_md: str,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    mid = _new_id("cmsg")
    now = time.time()
    conn.execute(
        "INSERT INTO chat_messages(message_id, thread_id, role, body_md, meta_json, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (
            mid,
            thread_id,
            role[:16],
            body_md[:8000],
            json.dumps(meta or {}, ensure_ascii=False),
            now,
        ),
    )
    conn.execute(
        "UPDATE chat_threads SET updated_at=? WHERE thread_id=?",
        (now, thread_id),
    )
    return mid


def chat_threads_list(conn, *, status: str = "open", limit: int = 30) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT thread_id, subject_id, status, topic, meta_json, created_at, updated_at "
        "FROM chat_threads WHERE status=? ORDER BY updated_at DESC LIMIT ?",
        (status, int(limit)),
    ).fetchall()
    return [_row_thread(r) for r in rows]


def chat_thread_get(conn, thread_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT thread_id, subject_id, status, topic, meta_json, created_at, updated_at "
        "FROM chat_threads WHERE thread_id=?",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    out = _row_thread(row)
    msgs = conn.execute(
        "SELECT message_id, thread_id, role, body_md, meta_json, created_at "
        "FROM chat_messages WHERE thread_id=? ORDER BY created_at ASC",
        (thread_id,),
    ).fetchall()
    out["messages"] = [_row_message(m) for m in msgs]
    return out


def chat_thread_resolve(conn, thread_id: str) -> bool:
    cur = conn.execute(
        "UPDATE chat_threads SET status='resolved', updated_at=? WHERE thread_id=?",
        (time.time(), thread_id),
    )
    return cur.rowcount > 0


def chat_open_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_threads WHERE status='open'"
    ).fetchone()
    return int(row["n"] if row else 0)


def _row_thread(row: Any) -> Dict[str, Any]:
    return {
        "thread_id": row["thread_id"],
        "subject_id": row["subject_id"],
        "status": row["status"],
        "topic": row["topic"],
        "meta": json.loads(row["meta_json"] or "{}"),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _row_message(row: Any) -> Dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "thread_id": row["thread_id"],
        "role": row["role"],
        "body_md": row["body_md"],
        "meta": json.loads(row["meta_json"] or "{}"),
        "created_at": float(row["created_at"]),
    }
