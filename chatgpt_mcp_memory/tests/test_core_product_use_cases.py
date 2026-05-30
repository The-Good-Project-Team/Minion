import json
import os
import time
from pathlib import Path

from ambient_scheduler import _run_once
from ingest import _embed, _get_model
from store import connect, seed_sync_sources, upsert_source


def _enable_auditable_local_mode(data_dir: Path) -> dict[str, str | None]:
    saved = {
        key: os.environ.get(key)
        for key in (
            "MINION_DATA_DIR",
            "MINION_DETERMINISTIC_EMBEDDINGS",
            "MINION_DISABLE_AMBIENT_SCHEDULER",
            "MINION_GEMINI_DISABLE_SECRET_FILES",
        )
    }
    os.environ["MINION_DATA_DIR"] = str(data_dir)
    os.environ["MINION_DETERMINISTIC_EMBEDDINGS"] = "1"
    os.environ["MINION_GEMINI_DISABLE_SECRET_FILES"] = "1"
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _reset_mcp_server(data_dir: Path) -> None:
    import mcp_server

    if mcp_server._CONN is not None:
        try:
            mcp_server._CONN.close()
        except Exception:
            pass
    mcp_server._CONN = None
    mcp_server._MODEL = None
    os.environ["MINION_DATA_DIR"] = str(data_dir)


def test_ambient_text_is_saved_as_vectorized_memory(tmp_path: Path) -> None:
    """Core app invariant: captured screen text must land in DB + vector index."""
    saved = _enable_auditable_local_mode(tmp_path)
    os.environ["MINION_DISABLE_AMBIENT_SCHEDULER"] = "1"

    stream = tmp_path / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "kind": "window_snapshot",
                "app_name": "Calendar",
                "window_title": "Reif planning",
                "window_id": "w-core",
                "ax_hash": "h-core",
                "ax_text_sample": "Reif has a Friday investor call with Jordan about the Minion identity companion.",
                "dedupe_key": "core-use-case-window",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    conn = connect(tmp_path / "memory.db")
    try:
        seed_sync_sources(conn)
        conn.commit()
        _run_once(tmp_path, lambda: conn)

        assert conn.execute("SELECT COUNT(*) FROM ambient_events").fetchone()[0] == 1
        source = conn.execute("SELECT kind, path FROM sources WHERE kind='ambient-ax'").fetchone()
        assert source is not None
        chunk = conn.execute(
            """
            SELECT c.rowid, c.text
            FROM chunks c
            JOIN sources s ON s.source_id = c.source_id
            WHERE s.kind = 'ambient-ax'
            """
        ).fetchone()
        assert chunk is not None
        assert "investor call with Jordan" in chunk["text"]
        assert conn.execute("SELECT COUNT(*) FROM vec_chunks WHERE rowid=?", (chunk["rowid"],)).fetchone()[0] == 1
    finally:
        conn.close()
        _restore_env(saved)


def test_mcp_retrieval_surfaces_user_identity_archive(tmp_path: Path) -> None:
    """Core MCP invariant: explicit self-archives must be retrievable about the user."""
    saved = _enable_auditable_local_mode(tmp_path)
    conn = connect(tmp_path / "memory.db")
    try:
        seed_sync_sources(conn)
        text = "Reif is building Minion as a private identity companion and prefers concise engineering updates."
        model = _get_model("deterministic")
        upsert_source(
            conn,
            path="exports/chatgpt/reif-profile.md",
            kind="chatgpt-export",
            sha256="core-profile",
            mtime=time.time(),
            bytes_=len(text),
            parser="test",
            source_meta={"title": "ChatGPT export profile"},
            chunks=[
                (
                    text,
                    "assistant",
                    {"source": "chatgpt-export"},
                )
            ],
            embeddings=_embed(model, [text]),
        )
        conn.commit()
        conn.close()
        _reset_mcp_server(tmp_path)
        import mcp_server

        hits = mcp_server._tool_ask_minion(
            {
                "query": "What is Reif building and how does he prefer updates?",
                "top_k": 3,
            }
        )
        assert hits["chunks"]
        assert "private identity companion" in hits["chunks"][0]["text"]
        assert "concise engineering updates" in hits["chunks"][0]["text"]
    finally:
        if not getattr(conn, "_closed", False):
            try:
                conn.close()
            except Exception:
                pass
        _restore_env(saved)


def test_ambient_text_promotes_to_graph_and_mcp_search(tmp_path: Path) -> None:
    """Core identity invariant: ambient text should become graph-searchable context."""
    saved = _enable_auditable_local_mode(tmp_path)
    os.environ.pop("MINION_DISABLE_AMBIENT_SCHEDULER", None)

    now = time.time()
    stream = tmp_path / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(
        json.dumps(
            {
                "ts": now,
                "kind": "window_snapshot",
                "app_name": "Mail",
                "window_title": "Jordan Lee — Minion identity companion",
                "window_id": "w-graph",
                "ax_hash": "h-graph",
                "ax_text_sample": "Jordan Lee jordan.lee@example.com is helping Reif test the Minion identity companion.",
                "dedupe_key": "core-use-case-graph",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    conn = connect(tmp_path / "memory.db")
    try:
        seed_sync_sources(conn)
        conn.commit()
        _run_once(tmp_path, lambda: conn)

        node = conn.execute(
            "SELECT node_id, source_refs_json FROM graph_nodes "
            "WHERE node_kind='person' AND title LIKE '%Jordan%'"
        ).fetchone()
        assert node is not None
        refs = json.loads(node["source_refs_json"] or "[]")
        assert any(str(ref).startswith("amb:") for ref in refs)
        assert any("ambient/screen/" in str(ref) for ref in refs)

        conn.commit()
        conn.close()
        _reset_mcp_server(tmp_path)
        import mcp_server

        hits = mcp_server._tool_ask_minion({"query": "What do we know about Jordan?", "top_k": 3})
        # Withheld: the graph fact is gated, surfaced as a release-request chunk pointer.
        assert hits["chunks"]
        assert hits["chunks"][0]["kind"] == "release-request"
        assert hits["chunks"][0]["release_required"] is True
        assert hits["chunks"][0]["release_level"] == 3
        assert "withheld until the user explicitly approves" in hits["chunks"][0]["text"]

        approved = mcp_server._tool_ask_minion(
            {
                "query": "What do we know about Jordan?",
                "top_k": 3,
                "release_ok": True,
                "approved_release_level": 3,
            }
        )
        # Approved: the Jordan entity now surfaces as a graph pointer (index model).
        assert approved["graph"]
        assert any(
            "Jordan" in (g.get("label", "") + " " + g.get("fact", "")) for g in approved["graph"]
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
        _restore_env(saved)
