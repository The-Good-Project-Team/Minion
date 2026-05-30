"""Screen-memory service tests."""
from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path

import pytest
import numpy as np

import screen_memory
from entity_resolution import ensure_person_node
from screen_memory import (
    create_task_from_recent_screen,
    fuse_screen_events,
    index_fused_screen_events,
    miyagi_guidance,
    remember_screen,
    screen_memory_status,
    summarize_last,
    verify_screen_memory_pipeline,
    what_was_i_doing,
    screen_search,
)
from store import (
    connect,
    graph_candidate_create,
    graph_candidate_list,
    screen_memory_events_since,
    seed_sync_sources,
    upsert_source,
)


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MINION_DISABLE_PLAYWRIGHT_DOM", "1")
    c = connect(tmp_path / "memory.db")
    seed_sync_sources(c)
    c.commit()
    yield c, tmp_path
    c.close()


def _write_stream(data_dir: Path, records: list[dict]) -> None:
    path = data_dir / "ambient" / "stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _gates(out: dict) -> dict:
    return {g["id"]: g for g in out["completion_gates"]["gates"]}


def test_remember_screen_ingests_stream_and_queues_graph(conn, monkeypatch) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "window_snapshot",
                "app_name": "Chrome",
                "window_title": "Stripe Dashboard",
                "window_id": "w1",
                "ax_hash": "h1",
                "ax_text_sample": "Payout history Export",
                "screenshot_inbox_rel": "screen-memory/missing.png",
            },
            {
                "ts": now,
                "kind": "screenshot_fallback",
                "app_name": "Chrome",
                "window_title": "Stripe Dashboard",
                "screenshot_inbox_rel": "screen-memory/missing.png",
            },
        ],
    )
    monkeypatch.setattr(screen_memory, "index_ax_from_stream", lambda **_: {"indexed": 1})
    queued = {}

    def fake_enqueue(conn, *, reason: str) -> None:
        queued["reason"] = reason

    import librarian_queue

    monkeypatch.setattr(librarian_queue, "enqueue_graph_infer", fake_enqueue)
    out = remember_screen(c, data_dir, index_events=False)
    assert out["ambient"]["ingested"] == 2
    assert out["ax_index"]["indexed"] == 1
    assert out["screenshot_ocr"]["attempted"] == 1
    assert out["screenshot_ocr"]["skipped"] == 1
    assert out["fused_events"]["upserted"] == 1
    fused_one = screen_memory_events_since(c, since_ts=0, limit=1)[0]
    assert (fused_one.get("raw") or {}).get("witness_count", 1) >= 2
    assert out["graph_candidates"]["created"] == 0
    assert out["graph_infer_queued"] is True
    assert queued["reason"] == "screen_memory"
    events = screen_memory_events_since(c, since_ts=0, limit=10)
    assert events[0]["scene"]
    assert events[0]["time"].endswith("Z")
    assert "T" in events[0]["time"]
    assert events[0]["trust_tier"] in {"dom_or_accessibility", "ocr"}


def test_verify_screen_memory_pipeline_acceptance(conn) -> None:
    c, data_dir = conn
    out = verify_screen_memory_pipeline(c, data_dir)
    assert out["ok"] is True
    assert {c["id"] for c in out["checks"] if c["ok"]} >= {
        "capture_ingest",
        "screenshot_ocr_index",
        "semantic_fusion",
        "witness_redundancy",
        "event_shape_time",
        "event_shape_temporal_refs",
        "confidence_tiers",
        "collector_coverage",
        "ocr_fallback",
        "contextual_click",
        "graph_candidate",
        "retrieval_index",
        "video_range_retrieval",
        "summary",
        "miyagi_guidance",
    }
    collectors: set[str] = set()
    for e in screen_memory_events_since(c, since_ts=0, limit=50):
        raw = e.get("raw") or {}
        collectors.update(raw.get("collector_types") or [])
    assert "general_vlm" in collectors
    assert out["retrieval"]["top_kind"] == "screen-event"
    assert out["guidance"]["mode"] == "graph_fill"


def test_summary_and_guidance_are_graph_first(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [{"ts": now, "kind": "window_focus", "app_name": "Sheets", "window_title": "Investor leads"}],
    )
    remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    summary = summarize_last(c, minutes=30)
    assert summary["event_count"] == 1
    assert "Investor leads" in summary["summary"]
    assert what_was_i_doing(c, minutes=30)["question"].startswith("what was I doing")

    graph_candidate_create(
        c,
        candidate_type="person_merge",
        title="Is Alex the same investor?",
        confidence=0.8,
    )
    c.commit()
    guidance = miyagi_guidance(c, data_dir)
    assert guidance["mode"] == "graph_fill"
    assert guidance["do_this"].startswith("Resolve this graph question")


def test_guidance_points_to_capture_probe_when_no_screen_evidence(conn, monkeypatch) -> None:
    import graph_fill

    c, data_dir = conn
    monkeypatch.setattr(graph_fill, "pick_next_gap", lambda *_args, **_kwargs: None)
    guidance = miyagi_guidance(c, data_dir)
    assert guidance["mode"] == "setup"
    assert guidance["blocker"] == "live_capture_probe"
    assert "screen-memory-status --probe" in guidance["do_this"]


def test_guidance_points_to_screen_recording_permission_blocker() -> None:
    status = {
        "readiness": {"warnings": ["screen_recording_permission_blocked"]},
        "completion_gates": {"gates": []},
    }
    guidance = screen_memory._guidance_from_status(status)
    assert guidance
    assert guidance["mode"] == "setup"
    assert guidance["blocker"] == "screen_recording_permission"
    assert "screen-memory-permissions" in guidance["do_this"]


def test_create_task_from_recent_screen(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "clipboard_event",
                "app_name": "Google Sheets",
                "window_title": "Investor leads",
                "summary": "User copied investor email alex@example.com",
                "text_excerpt": "alex@example.com",
                "detected_emails": ["alex@example.com"],
                "dedupe_key": "clipboard:alex@example.com",
            },
        ],
    )
    remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    out = create_task_from_recent_screen(c, minutes=30)
    assert out["created"] is True
    assert out["task"]["origin"] == "screen_memory"
    assert "Google Sheets" in out["task"]["title"]
    assert out["task"]["context_refs"][0]["kind"] == "screen_memory_event"
    assert out["task"]["context_refs"][0]["trust_tier"] == "user_events"


def test_screen_email_becomes_graph_candidate_and_guidance(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "clipboard_event",
                "app_name": "Google Sheets",
                "window_title": "Investor leads",
                "summary": "User copied investor email alex@example.com",
                "text_excerpt": "alex@example.com",
                "detected_emails": ["alex@example.com"],
                "dedupe_key": "clipboard:screen-graph-email",
            },
        ],
    )
    first = remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    second = remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    candidates = graph_candidate_list(c)
    assert first["graph_candidates"]["created"] == 1
    assert second["graph_candidates"]["created"] == 0
    assert candidates[0]["candidate_type"] == "screen_entity"
    assert candidates[0]["payload"]["email"] == "alex@example.com"
    assert candidates[0]["payload"]["screen_event_id"]
    assert candidates[0]["evidence_refs"][0].startswith("screen_event:")
    guidance = miyagi_guidance(c, data_dir)
    assert guidance["mode"] == "graph_fill"
    assert "alex@example.com" in guidance["do_this"]


def test_screen_named_email_becomes_graph_candidate(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "clipboard_event",
                "app_name": "Google Sheets",
                "window_title": "Investor leads",
                "summary": "User copied Alex Kim <alex@example.com>",
                "text_excerpt": "Alex Kim <alex@example.com>",
                "detected_emails": ["alex@example.com"],
                "dedupe_key": "clipboard:screen-graph-named-email",
            },
        ],
    )
    out = remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    candidates = graph_candidate_list(c)
    assert out["graph_candidates"]["created"] == 1
    assert candidates[0]["title"] == "Who is Alex Kim?"
    assert candidates[0]["payload"]["label"] == "Alex Kim"
    assert candidates[0]["payload"]["email"] == "alex@example.com"


def test_screen_message_identifier_skips_known_person(conn) -> None:
    c, data_dir = conn
    ensure_person_node(
        c,
        label="Alex Kim",
        meta={"imessage": "+1 (555) 123-9999", "source": "messages_export"},
    )
    c.commit()
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "window_focus",
                "app_name": "Messages",
                "window_title": "Alex Kim",
                "handle": "+1 (555) 123-9999",
                "dedupe_key": "messages:alex-known",
            },
        ],
    )
    out = remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    assert out["graph_candidates"]["scanned_entities"] == 1
    assert out["graph_candidates"]["created"] == 0
    assert graph_candidate_list(c) == []


def test_screen_message_identifier_matches_known_person_by_phone(conn) -> None:
    c, data_dir = conn
    ensure_person_node(
        c,
        label="Alex Kim",
        meta={"imessage": "+1 (555) 123-9999", "source": "messages_export"},
    )
    c.commit()
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "window_focus",
                "app_name": "Messages",
                "window_title": "A. Kim",
                "handle": "5551239999",
                "dedupe_key": "messages:alex-known-phone",
            },
        ],
    )
    out = remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    assert out["graph_candidates"]["scanned_entities"] == 1
    assert out["graph_candidates"]["created"] == 0
    assert graph_candidate_list(c) == []


def test_screen_message_identifier_creates_graph_candidate(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "window_focus",
                "app_name": "Messages",
                "window_title": "Jordan Lee",
                "handle": "+1 (555) 333-1212",
                "dedupe_key": "messages:jordan-new",
            },
        ],
    )
    out = remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    candidates = graph_candidate_list(c)
    assert out["graph_candidates"]["created"] == 1
    assert candidates[0]["title"] == "Who is Jordan Lee?"
    assert candidates[0]["payload"]["imessage"] == "5553331212"
    assert candidates[0]["payload"]["dedupe_key"] == "phone:5553331212"


def test_fuse_preserves_confidence_hierarchy(conn, monkeypatch) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "dom_snapshot",
                "app_name": "Chrome",
                "window_title": "Stripe Dashboard",
                "url": "https://stripe.com/dashboard/payouts",
                "visible_elements": [
                    {"role": "button", "label": "Export", "bounds": [812, 210, 96, 38], "source": "DOM", "confidence": 0.98}
                ],
                "dedupe_key": "dom:stripe:1",
            },
            {
                "ts": now + 1,
                "kind": "mouse_event",
                "app_name": "Chrome",
                "window_title": "Stripe Dashboard",
                "click_count": 1,
                "last_click": {"x": 830, "y": 220},
                "summary": "User clicked once in Chrome: Stripe Dashboard.",
                "dedupe_key": "mouse:stripe:export",
            },
            {
                "ts": now + 2,
                "kind": "clipboard_event",
                "app_name": "Chrome",
                "window_title": "Stripe Dashboard",
                "summary": "User copied investor email alex@example.com",
                "text_excerpt": "alex@example.com",
                "detected_emails": ["alex@example.com"],
                "dedupe_key": "clipboard:investor-email",
            },
            {
                "ts": now + 3,
                "kind": "rolling_video_clip",
                "app_name": "Chrome",
                "window_title": "Stripe Dashboard",
                "clip_path": str(data_dir / "ambient" / "video" / "clip.mov"),
                "duration_sec": 10,
                "dedupe_key": "clip:stripe:clip",
            },
            {
                "ts": now + 4,
                "kind": "marlin_event",
                "app_name": "Chrome",
                "window_title": "Stripe Dashboard",
                "scene": "User is reviewing payout history",
                "source_path": str(data_dir / "ambient" / "video" / "clip.mov"),
                "start_sec": 4,
                "end_sec": 9,
                "time_range": "4s-9s",
                "confidence": 0.81,
                "dedupe_key": "marlin:stripe:clip",
            },
        ],
    )
    remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    fused = screen_memory_events_since(c, since_ts=0, limit=10)
    tiers = {e["trust_tier"] for e in fused}
    assert {"dom_or_accessibility", "user_events", "temporal_video_events"} <= tiers
    clips = [
        e
        for e in fused
        if e["trust_tier"] == "temporal_video_events"
        or "rolling_video_clip" in ((e.get("raw") or {}).get("collector_types") or [])
    ]
    assert clips and (
        clips[0]["scene"].startswith("Recorded a")
        or "payout" in (clips[0].get("scene") or "").lower()
    )
    dom = next(e for e in fused if e["trust_tier"] == "dom_or_accessibility")
    assert dom["visible_elements"][0]["label"] == "Export"
    mouse = next(e for e in fused if e["raw"]["ambient_event_type"] == "mouse_event")
    assert mouse["visible_elements"][0]["label"] == "Export"
    assert mouse["events"][0]["source"] == "mouse_event + DOM"
    assert "Nearby UI target: button 'Export'" in mouse["events"][0]["summary"]
    clipboard = next(e for e in fused if e["raw"]["ambient_event_type"] == "clipboard_event")
    assert clipboard["events"][0]["summary"] == "User copied investor email alex@example.com"
    assert clipboard["raw"]["payload"]["detected_emails"] == ["alex@example.com"]
    marlin = next(e for e in fused if e["raw"]["ambient_event_type"] == "marlin_event")
    assert marlin["events"][0]["time_range"] == "4s-9s"
    assert marlin["time_range"] == "4s-9s"
    assert marlin["clip_path"].endswith("clip.mov")
    monkeypatch.setattr(screen_memory, "_get_model", lambda *_: object())
    monkeypatch.setattr(screen_memory, "_embed", lambda *_args, **_kwargs: np.ones((1, 384), dtype=np.float32))
    index_fused_screen_events(c)
    assert c.execute("SELECT 1 FROM chunks WHERE text LIKE '%4s-9s%' LIMIT 1").fetchone()
    out = screen_search(c, "payout history", app="Chrome", top_k=10)
    hits = out["hits"]
    assert any(h.get("time_range") == "4s-9s" for h in hits)
    matched_range = next(r for r in out["video_ranges"] if r.get("time_range") == "4s-9s")
    assert matched_range["clip_path"].endswith("clip.mov")
    assert matched_range["trust_tier"] == "temporal_video_events"
    assert fuse_screen_events(c)["upserted"] >= 3


def test_index_fused_screen_events_makes_clipboard_searchable(conn, monkeypatch) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "clipboard_event",
                "app_name": "Google Sheets",
                "window_title": "Investor leads",
                "summary": "User copied investor email alex@example.com",
                "text_excerpt": "alex@example.com",
                "detected_emails": ["alex@example.com"],
                "dedupe_key": "clipboard:investor-email-search",
            },
        ],
    )
    monkeypatch.setattr(screen_memory, "_get_model", lambda *_: object())
    monkeypatch.setattr(screen_memory, "_embed", lambda *_args, **_kwargs: np.ones((1, 384), dtype=np.float32))
    remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False)
    out = index_fused_screen_events(c)
    assert out["indexed"] >= 1
    rows = c.execute(
        "SELECT text FROM chunks WHERE text LIKE ? ORDER BY rowid DESC LIMIT 1",
        ("%alex@example.com%",),
    ).fetchall()
    assert rows
    assert "Investor leads" in rows[0]["text"]


def test_screen_memory_status_reports_setup_and_recent_evidence(conn) -> None:
    c, data_dir = conn
    now = time.time()
    _write_stream(
        data_dir,
        [
            {
                "ts": now,
                "kind": "dom_snapshot",
                "app_name": "Chrome",
                "window_title": "Stripe Dashboard",
                "visible_elements": [{"role": "button", "label": "Export"}],
                "dedupe_key": "dom:status",
            }
        ],
    )
    remember_screen(c, data_dir, index_ax=False, ingest_screenshots=False, index_events=False)
    out = screen_memory_status(c, data_dir, minutes=60)
    assert out["voice_default_off"] is True
    assert out["recent"]["raw_by_kind"]["dom_snapshot"] == 1
    assert out["recent"]["fused_by_trust"]["dom_or_accessibility"] == 1
    assert out["adapters"]["marlin"]["configured"] is False
    assert "marlin_adapter_not_configured" in out["readiness"]["warnings"]
    assert out["probe"]["ran"] is False
    gates = _gates(out)
    assert out["completion_gates"]["overall_ready"] is False
    assert gates["voice_default_off"]["status"] == "pass"
    assert gates["recent_raw_capture"]["status"] == "pass"
    assert gates["recent_fused_events"]["status"] == "pass"
    assert gates["recent_rolling_video_clips"]["status"] == "blocked"
    assert gates["indexed_screen_events"]["status"] == "blocked"
    assert out["recent"]["recent_screen_event_sources"] == 0
    assert gates["graph_fill_pipeline"]["status"] == "blocked"
    assert out["recent"]["screen_graph_candidates"] == 0
    assert out["recent"]["recent_screen_graph_candidates"] == 0
    assert gates["live_capture_probe"]["status"] == "unknown"
    assert gates["marlin_adapter"]["status"] == "blocked"
    assert gates["omniparser_adapter"]["status"] == "blocked"


def test_screen_memory_status_graph_gate_requires_recent_screen_candidate(conn) -> None:
    c, data_dir = conn
    cid = graph_candidate_create(
        c,
        candidate_type="screen_entity",
        title="Who is Old Lead?",
        body_md="Old screen-memory evidence.",
        payload={"email": "old@example.com"},
        evidence_refs=["screen_event:old"],
        confidence=0.5,
        source="screen_memory",
    )
    old = time.time() - 48 * 60 * 60
    c.execute(
        "UPDATE graph_candidates SET created_at=?, updated_at=? WHERE candidate_id=?",
        (old, old, cid),
    )
    c.commit()
    out = screen_memory_status(c, data_dir, minutes=60)
    gates = _gates(out)
    assert out["recent"]["open_graph_candidates"] == 1
    assert out["recent"]["screen_graph_candidates"] == 1
    assert out["recent"]["recent_screen_graph_candidates"] == 0
    assert gates["graph_fill_pipeline"]["status"] == "blocked"
    assert "0 recent / 1 total" in gates["graph_fill_pipeline"]["detail"]


def test_screen_memory_status_index_gate_requires_recent_screen_source(conn) -> None:
    c, data_dir = conn
    now = time.time()
    old = now - 48 * 60 * 60
    c.execute(
        "INSERT INTO sources(source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "src-old-screen",
            "ambient/screen-events/old.md",
            "screen-event",
            "sha-old",
            old,
            10,
            "screen_memory",
            "{}",
            old,
        ),
    )
    c.commit()
    out = screen_memory_status(c, data_dir, minutes=60)
    gates = _gates(out)
    assert out["recent"]["screen_event_sources"] == 1
    assert out["recent"]["recent_screen_event_sources"] == 0
    assert gates["indexed_screen_events"]["status"] == "blocked"
    assert "0 recent / 1 total" in gates["indexed_screen_events"]["detail"]


def test_screen_memory_status_index_gate_uses_event_time_not_index_time(conn) -> None:
    c, data_dir = conn
    now = time.time()
    old = now - 48 * 60 * 60
    c.execute(
        "INSERT INTO sources(source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "src-old-screen-new-index",
            "ambient/screen-events/old-new-index.md",
            "screen-event",
            "sha-old-new-index",
            old,
            10,
            "screen_memory",
            json.dumps({"occurred_at": old}),
            now,
        ),
    )
    c.commit()
    out = screen_memory_status(c, data_dir, minutes=60)
    gates = _gates(out)
    assert out["recent"]["screen_event_sources"] == 1
    assert out["recent"]["recent_screen_event_sources"] == 0
    assert gates["indexed_screen_events"]["status"] == "blocked"


def test_screen_memory_status_requires_recent_rolling_clip_file(conn) -> None:
    c, data_dir = conn
    out = screen_memory_status(c, data_dir, minutes=60)
    gates = _gates(out)
    assert gates["recent_rolling_video_clips"]["status"] == "blocked"
    assert gates["recent_rolling_video_clips"]["detail"] == "0 recent rolling video clips"

    video_dir = data_dir / "ambient" / "video"
    video_dir.mkdir(parents=True)
    (video_dir / "clip.mov").write_bytes(b"fake mov")
    out = screen_memory_status(c, data_dir, minutes=60)
    gates = _gates(out)
    assert out["recent"]["video_clips"][0]["path"].endswith("clip.mov")
    assert gates["recent_rolling_video_clips"]["status"] == "pass"


def test_screen_memory_status_probe_adds_probe_results(conn, monkeypatch) -> None:
    c, data_dir = conn
    monkeypatch.setattr(screen_memory, "_probe_screencapture", lambda _data: {"ok": True})
    monkeypatch.setattr(screen_memory, "_probe_rolling_video", lambda _data: {"ok": True, "duration_sec": 1})
    monkeypatch.setattr(screen_memory, "_probe_playwright_dom", lambda _data: {"ok": True, "visible_elements": 1})
    monkeypatch.setattr(screen_memory, "probe_screen_adapters", lambda _data: {"marlin": {"configured": False}, "omniparser": {"configured": False}})
    monkeypatch.setattr(screen_memory, "_probe_clipboard", lambda: {"ok": True, "content_captured": False})
    monkeypatch.setattr(screen_memory, "_probe_frontmost_app", lambda: {"ok": False, "error": "not authorized"})
    out = screen_memory_status(c, data_dir, minutes=60, run_probe=True)
    assert out["probe"]["ran"] is True
    assert out["probe"]["screencapture"]["ok"] is True
    assert out["probe"]["rolling_video"]["ok"] is True
    assert out["probe"]["playwright_dom"]["ok"] is True
    assert out["probe"]["clipboard"]["content_captured"] is False
    assert "frontmost_app_probe_failed" in out["readiness"]["warnings"]
    gates = _gates(out)
    assert gates["live_capture_probe"]["status"] == "blocked"
    assert "frontmost_app" in gates["live_capture_probe"]["detail"]
    assert gates["marlin_adapter"]["detail"] == "MINION_MARLIN_CMD not configured"


def test_adapter_gate_reports_failed_probe_stderr() -> None:
    gate = screen_memory._adapter_gate(
        "playwright_dom_adapter",
        "playwright_dom",
        {"playwright_dom": {"configured": True}},
        {"ran": True, "playwright_dom": {"ok": False, "stderr": "browser failed"}},
    )
    assert gate["status"] == "blocked"
    assert gate["detail"] == "browser failed"


def test_live_capture_probe_reports_screen_recording_hint(conn, monkeypatch) -> None:
    c, data_dir = conn
    monkeypatch.setattr(
        screen_memory,
        "_probe_screencapture",
        lambda _data: {
            "ok": False,
            "stderr": "could not create image from display 123",
            "hint": screen_memory._screen_capture_hint("could not create image from display 123"),
        },
    )
    monkeypatch.setattr(
        screen_memory,
        "_probe_rolling_video",
        lambda _data: {
            "ok": False,
            "stderr": "screencapture: capture error The operation could not be completed",
            "hint": screen_memory._screen_capture_hint(
                "screencapture: capture error The operation could not be completed",
                video=True,
            ),
        },
    )
    monkeypatch.setattr(screen_memory, "_probe_playwright_dom", lambda _data: {"ok": True})
    monkeypatch.setattr(screen_memory, "probe_screen_adapters", lambda _data: {"marlin": {"configured": False}, "omniparser": {"configured": False}})
    monkeypatch.setattr(screen_memory, "_probe_clipboard", lambda: {"ok": True, "content_captured": False})
    monkeypatch.setattr(screen_memory, "_probe_frontmost_app", lambda: {"ok": True})
    out = screen_memory_status(c, data_dir, minutes=60, run_probe=True)
    gates = _gates(out)
    assert gates["live_capture_probe"]["status"] == "blocked"
    assert "grant Screen Recording" in gates["live_capture_probe"]["detail"]
    assert "screen_recording_permission_blocked" in out["readiness"]["warnings"]


def test_clipboard_probe_treats_empty_clipboard_as_accessible(monkeypatch) -> None:
    class FakeProc:
        returncode = 1
        stdout = b""
        stderr = b""

    monkeypatch.setattr(screen_memory.Path, "exists", lambda self: True)
    monkeypatch.setattr(screen_memory.subprocess, "run", lambda *_args, **_kwargs: FakeProc())
    out = screen_memory._probe_clipboard()
    assert out["ok"] is True
    assert out["bytes"] == 0
    assert out["content_captured"] is False


def test_screen_search_filters_by_app_and_infers_yesterday(conn, monkeypatch) -> None:
    c, _data_dir = conn
    monkeypatch.setattr(screen_memory, "_get_model", lambda *_: object())
    monkeypatch.setattr(screen_memory, "_embed", lambda *_args, **_kwargs: np.ones((1, 384), dtype=np.float32))
    now = time.time()
    lt = time.localtime(now)
    today_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    yesterday = today_start - 3600
    today = today_start + 3600

    def add_screen_source(path: str, text: str, app: str, mtime: float) -> None:
        upsert_source(
            c,
            path=path,
            kind="screen-event",
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            mtime=mtime,
            bytes_=len(text),
            parser="screen_event",
            source_meta={"focus_app": app},
            chunks=[(text, "ambient", {"app": app, "create_time": mtime})],
            embeddings=np.ones((1, 384), dtype=np.float32),
        )

    add_screen_source("ambient/screen-events/yesterday/chrome.md", "Chrome Stripe Export button", "Chrome", yesterday)
    add_screen_source("ambient/screen-events/today/sheets.md", "Google Sheets investor email", "Google Sheets", today)
    c.commit()

    out = screen_search(c, "export button yesterday", app="Chrome", top_k=5)
    assert out["filters"]["time_window"] == "yesterday"
    assert out["hits"]
    assert all("chrome" in h["text"].casefold() for h in out["hits"])
