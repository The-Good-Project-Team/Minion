"""Auditable user-story tests: real screen work -> memory -> next action."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from screen_memory import (
    create_task_from_recent_screen,
    index_fused_screen_events,
    miyagi_guidance,
    remember_screen,
    what_was_i_doing,
)
from store import connect, graph_candidate_list, screen_memory_events_since, seed_sync_sources


def _write_stream(data_dir: Path, records: list[dict]) -> None:
    path = data_dir / "ambient" / "stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _enable_local_embeddings() -> str | None:
    old = os.environ.get("MINION_DETERMINISTIC_EMBEDDINGS")
    os.environ["MINION_DETERMINISTIC_EMBEDDINGS"] = "1"
    return old


def _restore_local_embeddings(old: str | None) -> None:
    if old is None:
        os.environ.pop("MINION_DETERMINISTIC_EMBEDDINGS", None)
    else:
        os.environ["MINION_DETERMINISTIC_EMBEDDINGS"] = old


def test_as_user_i_work_and_minion_tracks_it_and_tells_me_what_to_do_next(tmp_path: Path) -> None:
    """
    A user works on their computer; Minion should capture that work, remember it,
    and produce a next action without mocked services.
    """
    old_embed = _enable_local_embeddings()
    conn = connect(tmp_path / "memory.db")
    try:
        seed_sync_sources(conn)
        now = time.time()
        _write_stream(
            tmp_path,
            [
                {
                    "ts": now,
                    "kind": "window_snapshot",
                    "app_name": "Google Chrome",
                    "window_title": "The Good Project - Project Nohea investor update",
                    "window_id": "w-tgp-nohea",
                    "ax_hash": "h-tgp-nohea",
                    "ax_text_sample": (
                        "The Good Project Project Nohea investor update. "
                        "Prepare a founder update and send it to Alex Kim alex@example.com today."
                    ),
                    "dedupe_key": "user-story:tgp:nohea:window",
                },
                {
                    "ts": now + 2,
                    "kind": "clipboard_event",
                    "app_name": "Google Chrome",
                    "window_title": "The Good Project - Project Nohea investor update",
                    "summary": "User copied Alex Kim investor email alex@example.com",
                    "text_excerpt": "Alex Kim <alex@example.com>",
                    "detected_emails": ["alex@example.com"],
                    "dedupe_key": "user-story:tgp:nohea:clipboard",
                },
            ],
        )

        remembered = remember_screen(
            conn,
            tmp_path,
            index_ax=True,
            ingest_screenshots=False,
            run_adapters=False,
        )
        assert remembered["ambient"]["ingested"] == 2
        assert remembered["fused_events"]["upserted"] >= 1
        assert remembered["event_index"]["indexed"] >= 1

        events = screen_memory_events_since(conn, since_ts=now - 1, limit=10)
        assert events
        assert any("Project Nohea" in (e.get("scene") or "") for e in events)

        recall = what_was_i_doing(conn, minutes=30)
        recall_text = json.dumps(recall, ensure_ascii=False)
        assert "Project Nohea" in recall_text
        assert "The Good Project" in recall_text

        candidates = graph_candidate_list(conn, status="open", limit=10)
        assert any((c.get("payload") or {}).get("email") == "alex@example.com" for c in candidates)

        guidance = miyagi_guidance(conn, tmp_path, minutes=30)
        assert guidance["mode"] == "graph_fill"
        guidance_text = json.dumps(guidance, ensure_ascii=False)
        assert "Alex Kim" in guidance_text
        assert "alex@example.com" in guidance_text

        task = create_task_from_recent_screen(
            conn,
            minutes=30,
            title="Prepare Project Nohea investor update",
        )
        assert task["created"] is True
        assert task["task"]["origin"] == "screen_memory"
        assert "Project Nohea" in task["task"]["title"]
        assert task["task"]["context_refs"][0]["kind"] == "screen_memory_event"
    finally:
        conn.close()
        _restore_local_embeddings(old_embed)


def test_as_user_i_handle_family_work_and_minion_turns_it_into_a_next_step(tmp_path: Path) -> None:
    """A family planning session should become recallable screen memory and a task."""
    old_embed = _enable_local_embeddings()
    conn = connect(tmp_path / "memory.db")
    try:
        seed_sync_sources(conn)
        now = time.time()
        _write_stream(
            tmp_path,
            [
                {
                    "ts": now,
                    "kind": "window_snapshot",
                    "app_name": "Calendar",
                    "window_title": "Family summer plans - flights and checklist",
                    "window_id": "w-family-plans",
                    "ax_hash": "h-family-plans",
                    "ax_text_sample": (
                        "Family summer plans. Book flights, confirm hotel, and ask Tiffani "
                        "about Sunday dinner timing."
                    ),
                    "dedupe_key": "user-story:family:calendar",
                },
                {
                    "ts": now + 1,
                    "kind": "keyboard_event",
                    "app_name": "Calendar",
                    "window_title": "Family summer plans - flights and checklist",
                    "summary": "User edited family travel checklist",
                    "dedupe_key": "user-story:family:keyboard",
                },
            ],
        )

        remembered = remember_screen(
            conn,
            tmp_path,
            index_ax=True,
            ingest_screenshots=False,
            run_adapters=False,
        )
        assert remembered["ambient"]["ingested"] == 2
        assert remembered["fused_events"]["upserted"] >= 1

        recall = what_was_i_doing(conn, minutes=30)
        recall_text = json.dumps(recall, ensure_ascii=False)
        assert "Family summer plans" in recall_text
        assert "Calendar" in recall_text

        task = create_task_from_recent_screen(
            conn,
            minutes=30,
            title="Follow up on family summer plans",
        )
        assert task["created"] is True
        assert "family summer plans" in task["task"]["title"].lower()
        assert "Calendar" in json.dumps(task["task"]["context_refs"], ensure_ascii=False)
    finally:
        conn.close()
        _restore_local_embeddings(old_embed)


def test_as_agent_i_must_get_explicit_ok_before_releasing_current_work_context(tmp_path: Path) -> None:
    """MCP sees the level first; actual Level 3 work context requires approval."""
    old_embed = _enable_local_embeddings()
    old_data_dir = os.environ.get("MINION_DATA_DIR")
    conn = connect(tmp_path / "memory.db")
    try:
        seed_sync_sources(conn)
        now = time.time()
        _write_stream(
            tmp_path,
            [
                {
                    "ts": now,
                    "kind": "window_snapshot",
                    "app_name": "Google Chrome",
                    "window_title": "Project Castle Hill investor memo",
                    "window_id": "w-castle-hill",
                    "ax_hash": "h-castle-hill",
                    "ax_text_sample": (
                        "The Good Project Project Castle Hill investor memo. "
                        "Draft distribution plan and send capital raise update."
                    ),
                    "dedupe_key": "user-story:mcp-release:castle-hill",
                }
            ],
        )
        remembered = remember_screen(
            conn,
            tmp_path,
            index_ax=True,
            ingest_screenshots=False,
            run_adapters=False,
        )
        assert remembered["fused_events"]["upserted"] >= 1
        assert index_fused_screen_events(conn, since_hours=1)["indexed"] >= 1
        conn.commit()
        conn.close()

        os.environ["MINION_DATA_DIR"] = str(tmp_path)
        import mcp_server

        if mcp_server._CONN is not None:
            mcp_server._CONN.close()
        mcp_server._CONN = None
        mcp_server._MODEL = None

        first = mcp_server._tool_ask_minion(
            {"query": "What am I working on for Project Castle Hill?", "top_k": 4}
        )
        assert first["chunks"]
        assert first["chunks"][0]["kind"] == "release-request"
        assert first["chunks"][0]["release_level"] == 3
        assert "Project Castle Hill" not in first["chunks"][0]["text"]

        approved = mcp_server._tool_ask_minion(
            {
                "query": "What am I working on for Project Castle Hill?",
                "top_k": 4,
                "release_ok": True,
                "approved_release_level": 3,
            }
        )
        approved_text = json.dumps(approved, ensure_ascii=False)
        assert "Project Castle Hill" in approved_text
        assert any(h["kind"] == "screen-event" for h in approved["chunks"])
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if old_data_dir is None:
            os.environ.pop("MINION_DATA_DIR", None)
        else:
            os.environ["MINION_DATA_DIR"] = old_data_dir
        _restore_local_embeddings(old_embed)
