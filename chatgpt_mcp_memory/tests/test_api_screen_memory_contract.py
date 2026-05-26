from __future__ import annotations


def test_screen_memory_search_endpoint_preserves_video_ranges(monkeypatch) -> None:
    import api

    monkeypatch.setattr(api.State, "conn", classmethod(lambda cls: object()))

    def fake_screen_search(_conn, q, **kwargs):
        assert q == "when did I export payouts?"
        assert kwargs["app"] == "Chrome"
        return {
            "query": q,
            "filters": {"app": "Chrome"},
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

    monkeypatch.setattr(api, "screen_search", fake_screen_search)

    out = api.screen_memory_search("when did I export payouts?", top_k=3, app="Chrome")

    assert out["hits"][0]["time_range"] == "4s-9s"
    assert out["video_ranges"][0]["clip_path"].endswith("clip.mov")
