"""MCP screen-memory tool surface tests."""
from __future__ import annotations

from pathlib import Path


def test_mcp_exposes_screen_memory_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINION_DATA_DIR", str(tmp_path))
    import mcp_server

    mcp_server._CONN = None
    tools = {t["name"]: t for t in mcp_server.TOOLS}
    expected = {
        "remember_screen",
        "search_screen_memory",
        "summarize_last_screen",
        "what_was_i_doing",
        "screen_guidance",
        "screen_memory_status",
        "create_task_from_screen",
    }
    assert expected <= set(tools)
    assert expected <= set(mcp_server._DISPATCH)
    assert tools["search_screen_memory"]["title"] == "Search semantic screen memory"


def test_mcp_screen_summary_tool_uses_duration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINION_DATA_DIR", str(tmp_path))
    import mcp_server

    mcp_server._CONN = None
    try:
        out = mcp_server._tool_summarize_last_screen({"duration": "2h"})
        assert out["status"] == "ok"
        assert out["minutes"] == 120
        assert out["event_count"] == 0
    finally:
        if mcp_server._CONN is not None:
            mcp_server._CONN.close()
            mcp_server._CONN = None


def test_mcp_screen_search_exposes_video_ranges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINION_DATA_DIR", str(tmp_path))
    import mcp_server

    mcp_server._CONN = None
    monkeypatch.setattr(mcp_server, "_screen_context_tools_allowed", lambda: True)
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: object())

    def fake_screen_search(_conn, query, **kwargs):
        assert query == "when did I export payouts?"
        assert kwargs["app"] == "Chrome"
        return {
            "query": query,
            "hits": [
                {
                    "screen_event_id": "screen-1",
                    "text": "User exported payouts",
                    "time_range": "4s-9s",
                    "clip_path": "ambient/video/clip.mov",
                }
            ],
            "video_ranges": [
                {
                    "screen_event_id": "screen-1",
                    "time_range": "4s-9s",
                    "clip_path": "ambient/video/clip.mov",
                    "trust_tier": "temporal_video_events",
                }
            ],
        }

    monkeypatch.setattr(mcp_server, "screen_search", fake_screen_search)

    out = mcp_server._tool_search_screen_memory(
        {"query": "when did I export payouts?", "app": "Chrome", "top_k": 3}
    )

    assert out["status"] == "ok"
    assert out["count"] == 1
    assert out["video_ranges"][0]["time_range"] == "4s-9s"
    assert out["video_ranges"][0]["clip_path"].endswith("clip.mov")
