from __future__ import annotations

import json


def test_screen_memory_cli_search_prints_video_ranges(tmp_path, monkeypatch, capsys) -> None:
    import screen_memory_cli

    def fake_screen_search(_conn, query, **kwargs):
        assert query == "when did I export payouts?"
        assert kwargs["top_k"] == 3
        assert kwargs["app"] == "Chrome"
        return {
            "query": query,
            "hits": [{"text": "User exported payouts", "time_range": "4s-9s"}],
            "video_ranges": [
                {
                    "screen_event_id": "screen-1",
                    "time_range": "4s-9s",
                    "clip_path": "ambient/video/clip.mov",
                }
            ],
        }

    monkeypatch.setattr(screen_memory_cli, "screen_search", fake_screen_search)

    code = screen_memory_cli.main(
        [
            "--data-dir",
            str(tmp_path),
            "search",
            "when did I export payouts?",
            "--top-k",
            "3",
            "--app",
            "Chrome",
        ]
    )

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["video_ranges"][0]["time_range"] == "4s-9s"
    assert out["video_ranges"][0]["clip_path"].endswith("clip.mov")
