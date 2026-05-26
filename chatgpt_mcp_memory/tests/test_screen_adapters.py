"""External screen-adapter tests."""
from __future__ import annotations

import sys
from pathlib import Path

from screen_adapters import probe_screen_adapters, run_external_screen_adapters
from screen_memory import remember_screen
from store import connect, screen_memory_events_since, seed_sync_sources


def test_external_screen_adapters_append_and_fuse(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    inbox = tmp_path / "inbox" / "screen-memory"
    video_dir = data_dir / "ambient" / "video"
    inbox.mkdir(parents=True)
    video_dir.mkdir(parents=True)
    clip = video_dir / "clip.mp4"
    shot = inbox / "screen.png"
    clip.write_bytes(b"fake video")
    shot.write_bytes(b"fake image")
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        """
import json
import sys
path = sys.argv[1]
if path.endswith(".mp4"):
    print(json.dumps({
        "app_name": "Chrome",
        "window_title": "Payouts",
        "scene": "User is reviewing payout history",
        "start_time": 2,
        "end_time": 8.5,
        "events": [{"type": "review", "summary": "Payouts table is visible", "timestamp": 4, "duration": 2}],
        "confidence": 0.82
    }))
else:
    print(json.dumps({
        "app_name": "Chrome",
        "window_title": "Payouts",
        "elements": [{"role": "button", "label": "Export", "bbox": [10, 20, 80, 24]}],
        "confidence": 0.77
    }))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    cmd = f"{sys.executable} {adapter} {{input}}"
    monkeypatch.setenv("MINION_MARLIN_CMD", cmd)
    monkeypatch.setenv("MINION_OMNIPARSER_CMD", cmd)

    conn = connect(data_dir / "memory.db")
    seed_sync_sources(conn)
    conn.commit()
    try:
        out = remember_screen(conn, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
        assert out["adapters"]["appended"] == 2
        assert out["ambient"]["by_kind"]["marlin_event"] == 1
        assert out["ambient"]["by_kind"]["omniparser_parse"] == 1
        events = screen_memory_events_since(conn, since_ts=0, limit=10)
        tiers = {e["trust_tier"] for e in events}
        assert {"temporal_video_events", "visual_ui_parser"} <= tiers
        temporal = next(e for e in events if e["trust_tier"] == "temporal_video_events")
        assert temporal["raw"]["payload"]["time_range"] == "2s-8.5s"
        assert temporal["events"][0]["time_range"] == "4s-6s"
        visual = next(e for e in events if e["trust_tier"] == "visual_ui_parser")
        assert visual["visible_elements"][0]["label"] == "Export"
    finally:
        conn.close()


def test_external_screen_adapters_noop_without_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINION_MARLIN_CMD", raising=False)
    monkeypatch.delenv("MINION_OMNIPARSER_CMD", raising=False)
    monkeypatch.delenv("MINION_PLAYWRIGHT_DOM_CMD", raising=False)
    monkeypatch.setenv("MINION_DISABLE_PLAYWRIGHT_DOM", "1")
    out = run_external_screen_adapters(tmp_path / "data")
    assert out["appended"] == 0
    assert out["playwright_dom"]["configured"] is False
    assert out["marlin"]["configured"] is False
    assert out["omniparser"]["configured"] is False
    assert out["general_vlm"]["configured"] is False


def test_playwright_dom_adapter_appends_dom_snapshot(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    stream = data_dir / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text(
        '{"ts": 1, "kind": "browser_visit", "app_name": "Chrome", "window_title": "Demo", "url": "https://example.test/dashboard"}\n',
        encoding="utf-8",
    )
    adapter = tmp_path / "dom_adapter.py"
    adapter.write_text(
        """
import json
import sys
print(json.dumps({
    "app_name": "Chrome",
    "window_title": "Example dashboard",
    "url": sys.argv[1],
    "dom_text_sample": "Export payouts table",
    "visible_elements": [{"role": "button", "label": "Export", "source": "Playwright"}]
}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINION_PLAYWRIGHT_DOM_CMD", f"{sys.executable} {adapter} {{url}}")
    monkeypatch.delenv("MINION_DISABLE_PLAYWRIGHT_DOM", raising=False)
    monkeypatch.delenv("MINION_MARLIN_CMD", raising=False)
    monkeypatch.delenv("MINION_OMNIPARSER_CMD", raising=False)

    conn = connect(data_dir / "memory.db")
    seed_sync_sources(conn)
    conn.commit()
    try:
        out = remember_screen(conn, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
        assert out["adapters"]["playwright_dom"]["appended"] == 1
        assert out["ambient"]["by_kind"]["dom_snapshot"] == 1
        event = next(e for e in screen_memory_events_since(conn, since_ts=0, limit=10) if e["trust_tier"] == "dom_or_accessibility")
        assert event["visible_elements"][0]["source"] == "Playwright"
        assert event["visible_elements"][0]["label"] == "Export"
    finally:
        conn.close()


def test_general_vlm_adapter_appends_lowest_trust_event(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    inbox = tmp_path / "inbox" / "screen-memory"
    inbox.mkdir(parents=True)
    shot = inbox / "screen.png"
    shot.write_bytes(b"fake image")
    adapter = tmp_path / "vlm_adapter.py"
    adapter.write_text(
        """
import json
print(json.dumps({
    "caption": "Fallback model thinks this is a payout dashboard",
    "confidence": 0.31
}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINION_GENERAL_VLM_CMD", f"{sys.executable} {adapter} {{input}}")
    monkeypatch.delenv("MINION_PLAYWRIGHT_DOM_CMD", raising=False)
    monkeypatch.setenv("MINION_DISABLE_PLAYWRIGHT_DOM", "1")
    monkeypatch.delenv("MINION_MARLIN_CMD", raising=False)
    monkeypatch.delenv("MINION_OMNIPARSER_CMD", raising=False)

    conn = connect(data_dir / "memory.db")
    seed_sync_sources(conn)
    conn.commit()
    try:
        out = remember_screen(conn, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
        assert out["adapters"]["general_vlm"]["appended"] == 1
        assert out["ambient"]["by_kind"]["general_vlm"] == 1
        event = next(e for e in screen_memory_events_since(conn, since_ts=0, limit=10) if e["trust_tier"] == "general_vlm")
        assert event["scene"] == "Fallback model thinks this is a payout dashboard"
        assert event["confidence"] == 0.31
    finally:
        conn.close()


def test_probe_screen_adapters_validates_configured_commands(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    inbox = tmp_path / "inbox" / "screen-memory"
    video_dir = data_dir / "ambient" / "video"
    inbox.mkdir(parents=True)
    video_dir.mkdir(parents=True)
    (video_dir / "clip.mov").write_bytes(b"fake video")
    (inbox / "screen.png").write_bytes(b"fake image")
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        """
import json
import sys
path = sys.argv[1]
if path.endswith(".mov"):
    print(json.dumps({"scene": "User reviews payouts", "start_sec": 1, "end_sec": 3}))
else:
    print(json.dumps({
        "caption": "Fallback model sees a payout screen",
        "elements": [{"role": "button", "label": "Export"}]
    }))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    cmd = f"{sys.executable} {adapter} {{input}}"
    monkeypatch.setenv("MINION_MARLIN_CMD", cmd)
    monkeypatch.setenv("MINION_OMNIPARSER_CMD", cmd)
    monkeypatch.setenv("MINION_GENERAL_VLM_CMD", cmd)

    out = probe_screen_adapters(data_dir)
    assert out["marlin"]["ok"] is True
    assert out["marlin"]["sample"]["time_range"] == "1s-3s"
    assert out["omniparser"]["ok"] is True
    assert out["omniparser"]["sample"]["visible_elements"][0]["label"] == "Export"
    assert out["general_vlm"]["ok"] is True
    assert out["general_vlm"]["sample"]["scene"] == "Fallback model sees a payout screen"
    assert out["general_vlm"]["sample"]["confidence"] == 0.35
