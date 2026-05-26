"""Screen-first memory pipeline.

The useful MVP is computer memory, not computer control: normalize screen
events, index exact UI text first, OCR screenshot fallbacks when available, and
surface compact guidance for graph fill.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from ambient_index import index_ax_from_stream
from ambient_pipeline import ingest_ambient_jsonl
from ingest import DEFAULT_MODEL, _embed, _get_model, ingest_file
from store import (
    ambient_events_since,
    get_embed_dim,
    graph_candidate_create,
    graph_candidate_list,
    search as store_search,
    screen_memory_event_upsert,
    screen_memory_events_since,
    task_get,
    task_infer_insert,
    upsert_source,
)
from screen_adapters import probe_screen_adapters, run_external_screen_adapters, screen_adapter_status
from settings import load_settings


def remember_screen(
    conn,
    data_dir: Path,
    *,
    max_lines: int = 1200,
    index_ax: bool = True,
    index_events: bool = True,
    ingest_screenshots: bool = True,
    run_adapters: bool = True,
) -> Dict[str, Any]:
    """Run one screen-memory pass over ambient stream data."""
    data = Path(data_dir).expanduser().resolve()
    adapters = run_external_screen_adapters(data) if run_adapters else {
        "marlin": {"configured": False, "appended": 0},
        "omniparser": {"configured": False, "appended": 0},
        "appended": 0,
        "skipped": "disabled",
    }
    ambient = ingest_ambient_jsonl(data_dir=data, conn=conn, max_lines=max_lines)
    conn.commit()
    ax = {"indexed": 0, "skipped": "disabled"}
    if index_ax:
        ax = index_ax_from_stream(data_dir=data, conn=conn, dry_run=False)
        conn.commit()
    shots = _ingest_screenshot_fallbacks(conn, data) if ingest_screenshots else {
        "attempted": 0,
        "indexed": 0,
        "skipped": 0,
        "items": [],
    }
    conn.commit()
    fused = fuse_screen_events(conn)
    conn.commit()
    graph_candidates = create_graph_candidates_from_screen_events(conn)
    conn.commit()
    event_index = index_fused_screen_events(conn) if index_events else {
        "indexed": 0,
        "skipped": "disabled",
    }
    conn.commit()
    graph_signal = bool(
        int(ambient.get("ingested", 0) or 0)
        or int(ax.get("indexed", 0) or 0)
        or int(shots.get("indexed", 0) or 0)
        or int(fused.get("upserted", 0) or 0)
        or int(graph_candidates.get("created", 0) or 0)
        or int(event_index.get("indexed", 0) or 0)
    )
    if graph_signal:
        try:
            from forty_two_queue import enqueue_graph_infer

            enqueue_graph_infer(conn, reason="screen_memory")
            conn.commit()
            graph_queued = True
        except Exception:
            graph_queued = False
    else:
        graph_queued = False
    return {
        "ambient": ambient,
        "adapters": adapters,
        "ax_index": ax,
        "screenshot_ocr": shots,
        "fused_events": fused,
        "graph_candidates": graph_candidates,
        "event_index": event_index,
        "graph_infer_queued": graph_queued,
        "trust_order": [
            "dom_or_accessibility",
            "user_events",
            "temporal_video_events",
            "visual_ui_parser",
            "ocr",
            "general_vlm",
        ],
    }


def verify_screen_memory_pipeline(conn, data_dir: Path) -> Dict[str, Any]:
    """Run a deterministic synthetic acceptance check without live capture."""
    data = Path(data_dir).expanduser().resolve()
    now = time.time()
    _write_verify_screenshot(data)
    stream = data / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "ts": now - 4,
            "kind": "dom_snapshot",
            "app_name": "Chrome",
            "window_title": "Stripe Dashboard",
            "url": "https://stripe.com/dashboard/payouts",
            "dom_text_sample": "Payout history Export",
            "visible_elements": [{"role": "button", "label": "Export", "source": "DOM"}],
            "dedupe_key": f"verify:dom:{now}",
        },
        {
            "ts": now - 3,
            "kind": "mouse_event",
            "app_name": "Chrome",
            "window_title": "Stripe Dashboard",
            "summary": "User clicked Export",
            "last_click": {"x": 812, "y": 210},
            "dedupe_key": f"verify:mouse:{now}",
        },
        {
            "ts": now - 2,
            "kind": "clipboard_event",
            "app_name": "Google Sheets",
            "window_title": "Investor leads",
            "summary": "User copied Alex Kim <alex@example.com>",
            "text_excerpt": "Alex Kim <alex@example.com>",
            "detected_emails": ["alex@example.com"],
            "dedupe_key": f"verify:clipboard:{now}",
        },
        {
            "ts": now - 1,
            "kind": "screenshot_fallback",
            "app_name": "Chrome",
            "window_title": "Stripe Dashboard",
            "summary": "OCR fallback saw payout history text",
            "screenshot_inbox_rel": "screen-memory/verify.png",
            "dedupe_key": f"verify:ocr:{now}",
        },
        {
            "ts": now - 0.5,
            "kind": "marlin_event",
            "app_name": "Chrome",
            "window_title": "Stripe Dashboard",
            "summary": "User reviewed payouts and exported a report",
            "time_range": "00:02-00:08",
            "clip_path": "ambient/video/verify.mov",
            "dedupe_key": f"verify:marlin:{now}",
        },
        {
            "ts": now,
            "kind": "omniparser_parse",
            "app_name": "Chrome",
            "window_title": "Stripe Dashboard",
            "visible_elements": [{"role": "button", "label": "Export CSV", "source": "OmniParser"}],
            "dedupe_key": f"verify:omniparser:{now}",
        },
        {
            "ts": now + 0.5,
            "kind": "general_vlm",
            "app_name": "Chrome",
            "window_title": "Stripe Dashboard",
            "summary": "Fallback visual model guessed a dashboard workflow.",
            "confidence": 0.36,
            "dedupe_key": f"verify:vlm:{now}",
        },
    ]
    with stream.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    remember = remember_screen(
        conn,
        data,
        index_ax=False,
        ingest_screenshots=True,
        run_adapters=False,
        index_events=False,
    )
    events = screen_memory_events_since(conn, since_ts=now - 60, limit=50)
    event_index = _verify_index_one_screen_event(conn, events)
    video_index = _verify_index_one_temporal_screen_event(conn, events)
    retrieval_hits = store_search(
        conn,
        event_index["query_vec"],
        top_k=3,
        kind="screen-event",
        path_glob="ambient/*",
    ) if event_index.get("indexed") else []
    video_hits = store_search(
        conn,
        video_index["query_vec"],
        top_k=3,
        kind="screen-event",
        path_glob="ambient/*",
    ) if video_index.get("indexed") else []
    video_ranges = _screen_search_video_ranges([_screen_search_hit(h) for h in video_hits])
    summary = summarize_last(conn, minutes=10)
    guidance = miyagi_guidance(conn, data, minutes=10)
    status = screen_memory_status(conn, data, minutes=10, run_probe=False)
    candidates = graph_candidate_list(conn, status="open", limit=5)

    tiers = {str(e.get("trust_tier") or "") for e in events}
    checks = [
        ("capture_ingest", int(remember["ambient"].get("ingested") or 0) >= len(records)),
        ("screenshot_ocr_index", int(remember["screenshot_ocr"].get("indexed") or 0) >= 1),
        ("semantic_fusion", int(remember["fused_events"].get("upserted") or 0) >= len(records)),
        ("event_shape_time", any(str(e.get("time") or "").endswith("Z") and "T" in str(e.get("time") or "") for e in events)),
        ("event_shape_temporal_refs", any(e.get("time_range") == "00:02-00:08" and e.get("clip_path") == "ambient/video/verify.mov" for e in events)),
        ("confidence_tiers", {"dom_or_accessibility", "user_events", "temporal_video_events", "visual_ui_parser", "ocr", "general_vlm"}.issubset(tiers)),
        ("ocr_fallback", any(e.get("trust_tier") == "ocr" for e in events)),
        ("contextual_click", any("DOM" in str(a.get("source") or "") for e in events for a in (e.get("events") or []))),
        ("graph_candidate", any((c.get("payload") or {}).get("email") == "alex@example.com" for c in candidates)),
        ("retrieval_index", bool(retrieval_hits) and retrieval_hits[0].kind == "screen-event"),
        ("video_range_retrieval", any(r.get("time_range") == "00:02-00:08" and r.get("clip_path") == "ambient/video/verify.mov" for r in video_ranges)),
        ("summary", int(summary.get("event_count") or 0) > 0),
        ("miyagi_guidance", guidance.get("mode") == "graph_fill"),
    ]
    check_rows = [{"id": cid, "ok": bool(ok)} for cid, ok in checks]
    return {
        "ok": all(c["ok"] for c in check_rows),
        "checks": check_rows,
        "remember": remember,
        "event_count": len(events),
        "trust_tiers": sorted(tiers),
        "retrieval": {
            "indexed": bool(event_index.get("indexed")),
            "top_path": retrieval_hits[0].path if retrieval_hits else "",
            "top_kind": retrieval_hits[0].kind if retrieval_hits else "",
            "video_ranges": video_ranges,
        },
        "graph_candidates": candidates[:3],
        "guidance": guidance,
        "status": status,
    }


def fuse_screen_events(conn, *, since_hours: float = 6.0, limit: int = 500) -> Dict[str, Any]:
    """Normalize raw ambient rows into semantic screen-memory records."""
    since = time.time() - max(0.1, float(since_hours)) * 3600.0
    rows = ambient_events_since(conn, since_ts=since, limit=limit)
    upserted = 0
    by_trust: Dict[str, int] = {}
    contexts: Dict[tuple[str, str], Dict[str, Any]] = {}
    for e in sorted(rows, key=lambda r: float(r.get("captured_at") or 0.0)):
        fused = _fuse_ambient_event(e)
        if not fused:
            continue
        raw_type = ((fused.get("raw") or {}).get("ambient_event_type") or "") if isinstance(fused.get("raw"), dict) else ""
        if fused.get("trust_tier") == "user_events" and raw_type in ("mouse_event", "keyboard_event"):
            ctx = _matching_screen_context(contexts, fused)
            if ctx:
                _merge_user_event_context(fused, ctx)
        screen_memory_event_upsert(conn, **fused)
        upserted += 1
        tier = str(fused.get("trust_tier") or "unknown")
        by_trust[tier] = by_trust.get(tier, 0) + 1
        _remember_screen_context(contexts, fused)
    return {"upserted": upserted, "scanned": len(rows), "by_trust": by_trust}


def index_fused_screen_events(conn, *, since_hours: float = 6.0, limit: int = 300) -> Dict[str, Any]:
    """Index fused semantic screen events into vector/FTS search chunks."""
    since = time.time() - max(0.1, float(since_hours)) * 3600.0
    rows = screen_memory_events_since(conn, since_ts=since, limit=limit)
    if not rows:
        return {"indexed": 0, "scanned": 0}
    model = _get_model(DEFAULT_MODEL)
    indexed = 0
    for e in rows:
        body = _event_search_document(e)
        if not body.strip():
            continue
        ts = float(e.get("occurred_at") or time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        event_id = str(e.get("event_id") or f"event-{indexed}")
        rel_path = f"ambient/screen-events/{day}/{event_id}.md"
        vecs = _embed(model, [body], on_progress=lambda *_: None)
        upsert_source(
            conn,
            path=rel_path,
            kind="screen-event",
            sha256=_sha256_text(body),
            mtime=ts,
            bytes_=len(body.encode("utf-8")),
            parser="screen_event",
            source_meta={
                "retention_days": 14,
                "screen_event_id": event_id,
                "occurred_at": ts,
                "trust_tier": e.get("trust_tier"),
                "focus_app": e.get("app"),
                "focus_title": e.get("window"),
                "url": e.get("url"),
                "time_range": _screen_event_time_range(e),
                "clip_path": _screen_event_clip_path(e),
            },
            chunks=[
                (
                    body,
                    "ambient",
                    {
                        "create_time": ts,
                        "screen_event_id": event_id,
                        "trust_tier": e.get("trust_tier"),
                        "app": e.get("app"),
                        "window": e.get("window"),
                        "time_range": _screen_event_time_range(e),
                        "clip_path": _screen_event_clip_path(e),
                    },
                )
            ],
            embeddings=vecs,
        )
        indexed += 1
    return {"indexed": indexed, "scanned": len(rows)}


def create_graph_candidates_from_screen_events(
    conn,
    *,
    since_hours: float = 6.0,
    limit: int = 300,
) -> Dict[str, Any]:
    """Surface high-confidence screen entities as graph-fill questions."""
    since = time.time() - max(0.1, float(since_hours)) * 3600.0
    rows = screen_memory_events_since(conn, since_ts=since, limit=limit)
    existing = _open_screen_entity_keys(conn)
    created = 0
    scanned_entities = 0
    for e in rows:
        for entity in _screen_entities_from_event(e):
            key = str(entity.get("dedupe_key") or "").strip()
            if not key:
                continue
            scanned_entities += 1
            if key in existing or _screen_entity_known_in_graph(conn, entity):
                continue
            title = _screen_entity_title(entity)
            app = str(e.get("app") or "").strip()
            window = str(e.get("window") or "").strip()
            where = "screen memory"
            if app and window:
                where = f"{app}: {window}"
            elif app:
                where = app
            descriptor = _screen_entity_descriptor(entity)
            graph_candidate_create(
                conn,
                candidate_type="screen_entity",
                title=title,
                body_md=(
                    f"Screen memory saw {descriptor} in {where}. "
                    "Tell Minion who this is so it can fill the graph."
                ),
                payload={
                    **entity,
                    "app": app,
                    "window": window,
                    "url": e.get("url"),
                    "screen_event_id": e.get("event_id"),
                    "trust_tier": e.get("trust_tier"),
                },
                evidence_refs=_screen_event_refs(e),
                confidence=0.72,
                source="screen_memory",
            )
            existing.add(key)
            created += 1
    return {"created": created, "scanned_events": len(rows), "scanned_entities": scanned_entities}


def summarize_last(conn, *, minutes: int = 30, limit: int = 120) -> Dict[str, Any]:
    """Summarize recent screen context without calling an LLM."""
    mins = max(1, min(int(minutes), 24 * 60))
    since = time.time() - mins * 60.0
    events = screen_memory_events_since(conn, since_ts=since, limit=limit)
    windows: List[Dict[str, Any]] = []
    app_counts: Dict[str, int] = {}
    for e in events:
        app = str(e.get("app") or "").strip()
        title = str(e.get("window") or "").strip()
        if app:
            app_counts[app] = app_counts.get(app, 0) + 1
        if title or app:
            row = {
                "time": e.get("occurred_at"),
                "type": e.get("trust_tier"),
                "app": app,
                "window": title,
                "url": e.get("url"),
                "scene": e.get("scene"),
                "confidence": e.get("confidence"),
            }
            if row not in windows:
                windows.append(row)
    top_apps = [
        {"app": app, "events": count}
        for app, count in sorted(app_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    ]
    return {
        "minutes": mins,
        "event_count": len(events),
        "top_apps": top_apps,
        "recent_windows": windows[:12],
        "semantic_events": events[:8],
        "summary": _summary_sentence(top_apps, windows),
    }


def what_was_i_doing(conn, *, minutes: int = 20) -> Dict[str, Any]:
    out = summarize_last(conn, minutes=minutes)
    out["question"] = f"what was I doing in the last {out['minutes']} minutes?"
    return out


def screen_memory_status(
    conn,
    data_dir: Path,
    *,
    minutes: int = 60,
    run_probe: bool = False,
) -> Dict[str, Any]:
    """Inspect screen-memory readiness and recent evidence without capturing."""
    data = Path(data_dir).expanduser().resolve()
    mins = max(1, min(int(minutes), 24 * 60))
    since = time.time() - mins * 60.0
    settings = load_settings(data)
    collectors = settings.get("ambient_collectors") if isinstance(settings, dict) else {}
    if not isinstance(collectors, dict):
        collectors = {}
    ambient_rows = ambient_events_since(conn, since_ts=since, limit=1000)
    fused_rows = screen_memory_events_since(conn, since_ts=since, limit=1000)
    raw_by_kind: Dict[str, int] = {}
    for row in ambient_rows:
        kind = str(row.get("event_type") or "unknown")
        raw_by_kind[kind] = raw_by_kind.get(kind, 0) + 1
    fused_by_trust: Dict[str, int] = {}
    for row in fused_rows:
        tier = str(row.get("trust_tier") or "unknown")
        fused_by_trust[tier] = fused_by_trust.get(tier, 0) + 1
    clips = _recent_files(data / "ambient" / "video", {".mov", ".mp4", ".m4v", ".webm"}, limit=8)
    screenshots = _recent_files(data.parent / "inbox" / "screen-memory", {".png", ".jpg", ".jpeg", ".webp"}, limit=8)
    status = {
        "minutes": mins,
        "data_dir": str(data),
        "stream_paths": {
            "ambient": str(data / "ambient" / "stream.jsonl"),
            "legacy": str(data / "screen_context" / "stream.jsonl"),
            "ambient_exists": (data / "ambient" / "stream.jsonl").is_file(),
            "legacy_exists": (data / "screen_context" / "stream.jsonl").is_file(),
        },
        "collectors": collectors,
        "voice_default_off": not bool(collectors.get("listening")) and not bool(collectors.get("full_listening")),
        "adapters": screen_adapter_status(data),
        "recent": {
            "raw_event_count": len(ambient_rows),
            "raw_by_kind": raw_by_kind,
            "fused_event_count": len(fused_rows),
            "fused_by_trust": fused_by_trust,
            "video_clips": clips,
            "screenshots": screenshots,
            "screen_event_sources": _count_screen_event_sources(conn),
            "recent_screen_event_sources": _count_screen_event_sources(conn, since_ts=since),
            "open_graph_candidates": len(graph_candidate_list(conn, status="open", limit=200)),
            "screen_graph_candidates": _count_screen_graph_candidates(conn),
            "recent_screen_graph_candidates": _count_screen_graph_candidates(conn, since_ts=since),
        },
    }
    status["probe"] = _live_screen_probe(data) if run_probe else {"ran": False}
    status["readiness"] = _screen_memory_readiness(status)
    status["completion_gates"] = _screen_memory_completion_gates(status)
    return status


def create_task_from_recent_screen(
    conn,
    *,
    minutes: int = 20,
    title: str = "",
) -> Dict[str, Any]:
    """Create one inferred task from recent fused screen memory."""
    summary = summarize_last(conn, minutes=minutes)
    events = summary.get("semantic_events") or []
    if not events:
        return {"created": False, "reason": "no_recent_screen_memory", "summary": summary}
    task_title = title.strip() or _task_title_from_summary(summary)
    refs = [
        {
            "kind": "screen_memory_event",
            "event_id": e.get("event_id"),
            "trust_tier": e.get("trust_tier"),
            "app": e.get("app"),
            "window": e.get("window"),
        }
        for e in events[:8]
    ]
    body_lines = [
        "Created from recent screen memory.",
        "",
        f"Summary: {summary.get('summary')}",
    ]
    first = events[0]
    if first.get("scene"):
        body_lines.extend(["", f"Scene: {first.get('scene')}"])
    tid = task_infer_insert(
        conn,
        title=task_title,
        body_md="\n".join(body_lines),
        origin="screen_memory",
        priority="normal",
        context_refs=refs,
    )
    conn.commit()
    return {
        "created": True,
        "task_id": tid,
        "task": task_get(conn, tid),
        "summary": summary,
    }


def screen_search(
    conn,
    query: str,
    *,
    top_k: int = 8,
    app: str = "",
    after: Optional[float] = None,
    before: Optional[float] = None,
) -> Dict[str, Any]:
    """Semantic search scoped to screen-derived memory."""
    q = query.strip()
    if not q:
        return {"query": q, "hits": []}
    inferred_after, inferred_before, inferred_label = _infer_query_time_window(q)
    if after is None:
        after = inferred_after
    if before is None:
        before = inferred_before
    model = _get_model(DEFAULT_MODEL)
    vec = _embed(model, [q], on_progress=lambda *_: None)[0]
    # Store.search already over-fetches internally; keep this second-stage
    # pool bounded while leaving room for app filtering after vector search.
    internal_k = min(max(top_k * 2, 32), 40)
    hits = store_search(
        conn,
        vec,
        top_k=internal_k,
        path_glob="ambient/*",
        since=after,
        before=before,
    )
    app_filter = app.strip().casefold()
    if app_filter:
        hits = [
            h for h in hits
            if app_filter in str(h.meta.get("app") or h.source_meta.get("focus_app") or "").casefold()
        ]
    hits = hits[:top_k]
    rendered_hits = [_screen_search_hit(h) for h in hits]
    return {
        "query": q,
        "filters": {
            "app": app.strip() or None,
            "after": after,
            "before": before,
            "time_window": inferred_label,
        },
        "hits": rendered_hits,
        "video_ranges": _screen_search_video_ranges(rendered_hits),
    }


def _screen_search_hit(h: Any) -> Dict[str, Any]:
    return {
        "chunk_id": h.chunk_id,
        "score": h.score,
        "path": h.path,
        "kind": h.kind,
        "text": h.text[:500],
        "screen_event_id": h.meta.get("screen_event_id") or h.source_meta.get("screen_event_id"),
        "app": h.meta.get("app") or h.source_meta.get("focus_app"),
        "window": h.meta.get("window") or h.source_meta.get("focus_title"),
        "trust_tier": h.meta.get("trust_tier") or h.source_meta.get("trust_tier"),
        "time_range": h.meta.get("time_range") or h.source_meta.get("time_range"),
        "clip_path": h.meta.get("clip_path") or h.source_meta.get("clip_path"),
    }


def _screen_search_video_ranges(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranges: List[Dict[str, Any]] = []
    for h in hits:
        time_range = str(h.get("time_range") or "").strip()
        clip_path = str(h.get("clip_path") or "").strip()
        if not time_range and not clip_path:
            continue
        ranges.append(
            {
                "screen_event_id": h.get("screen_event_id"),
                "score": h.get("score"),
                "path": h.get("path"),
                "kind": h.get("kind"),
                "app": h.get("app"),
                "window": h.get("window"),
                "trust_tier": h.get("trust_tier"),
                "time_range": time_range or None,
                "clip_path": clip_path or None,
                "text": str(h.get("text") or "")[:500],
            }
        )
    return ranges


def miyagi_guidance(conn, data_dir: Path, *, minutes: int = 30) -> Dict[str, Any]:
    """Rule-based next action: graph-fill first, recent screen context second."""
    candidates = graph_candidate_list(conn, status="open", limit=3)
    if candidates:
        c = candidates[0]
        return {
            "mode": "graph_fill",
            "do_this": f"Resolve this graph question: {c.get('title')}",
            "candidate": c,
        }
    try:
        from graph_fill import pick_next_gap

        gap = pick_next_gap(conn, Path(data_dir))
    except Exception:
        gap = None
    if gap:
        label = gap.get("label") or gap.get("bucket_label") or gap.get("kind") or "this gap"
        return {
            "mode": "graph_gap",
            "do_this": f"Answer one line about {label} so Minion can fill the graph.",
            "gap": gap,
        }
    recent = summarize_last(conn, minutes=minutes)
    if recent["event_count"]:
        return {
            "mode": "screen_memory",
            "do_this": "Convert the last screen session into one project update or task.",
            "recent": recent,
        }
    status = screen_memory_status(conn, data_dir, minutes=minutes, run_probe=False)
    setup = _guidance_from_status(status)
    if setup:
        setup["recent"] = recent
        return setup
    return {
        "mode": "idle",
        "do_this": "Turn on screen memory or keep working; Minion needs fresh screen evidence.",
        "recent": recent,
    }


def _guidance_from_status(status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    warnings = set(str(w) for w in ((status.get("readiness") or {}).get("warnings") or []))
    gates = {str(g.get("id")): g for g in ((status.get("completion_gates") or {}).get("gates") or [])}
    if "screen_recording_permission_blocked" in warnings:
        return {
            "mode": "setup",
            "do_this": "Run `minion screen-memory-permissions`, grant Screen Recording, then run `minion screen-memory-status --probe`.",
            "blocker": "screen_recording_permission",
            "status": status,
        }
    if gates.get("live_capture_probe", {}).get("status") == "unknown":
        return {
            "mode": "setup",
            "do_this": "Run `minion screen-memory-status --probe` to validate screen, DOM, clipboard, and app capture.",
            "blocker": "live_capture_probe",
            "status": status,
        }
    if "no_recent_raw_ambient_events" in warnings:
        return {
            "mode": "setup",
            "do_this": "Start Minion desktop and let ambient capture collect fresh screen evidence, then run `minion remember-screen`.",
            "blocker": "no_recent_screen_evidence",
            "status": status,
        }
    if "marlin_adapter_not_configured" in warnings or "omniparser_adapter_not_configured" in warnings:
        return {
            "mode": "setup",
            "do_this": "Configure `MINION_MARLIN_CMD` and `MINION_OMNIPARSER_CMD`, then run `minion screen-memory-status --probe`.",
            "blocker": "screen_adapters",
            "status": status,
        }
    return None


def _summary_sentence(top_apps: List[Dict[str, Any]], windows: List[Dict[str, Any]]) -> str:
    if not windows:
        return "No recent screen memory events found."
    app = top_apps[0]["app"] if top_apps else windows[0].get("app") or "apps"
    title = windows[0].get("window") or ""
    if title:
        return f"Most recent screen activity was in {app}: {title}."
    return f"Most recent screen activity was in {app}."


def _recent_files(root: Path, exts: set[str], *, limit: int) -> List[Dict[str, Any]]:
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    out: List[Dict[str, Any]] = []
    for p in files[: max(1, limit)]:
        try:
            st = p.stat()
            out.append({"path": str(p), "bytes": st.st_size, "mtime": st.st_mtime})
        except OSError:
            continue
    return out


def _count_screen_event_sources(conn, *, since_ts: Optional[float] = None) -> int:
    try:
        if since_ts is not None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sources WHERE kind = 'screen-event' AND mtime >= ?",
                (float(since_ts),),
            ).fetchone()
            return int(row["n"] or 0) if row else 0
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE kind = 'screen-event'"
        ).fetchone()
        return int(row["n"] or 0) if row else 0
    except Exception:
        return 0


def _count_screen_graph_candidates(conn, *, since_ts: Optional[float] = None) -> int:
    try:
        if since_ts is not None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM graph_candidates "
                "WHERE source = 'screen_memory' AND created_at >= ?",
                (float(since_ts),),
            ).fetchone()
            return int(row["n"] if row else 0)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM graph_candidates WHERE source = 'screen_memory'"
        ).fetchone()
        return int(row["n"] if row else 0)
    except Exception:
        return 0


def _verify_index_one_screen_event(conn, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    for e in events:
        body = _event_search_document(e)
        if "Export" not in body and "alex@example.com" not in body:
            continue
        dim = get_embed_dim(conn)
        vec = np.ones((1, dim), dtype=np.float32)
        ts = float(e.get("occurred_at") or time.time())
        event_id = str(e.get("event_id") or "verify")
        upsert_source(
            conn,
            path=f"ambient/screen-events/verify/{event_id}.md",
            kind="screen-event",
            sha256=_sha256_text(body),
            mtime=ts,
            bytes_=len(body.encode("utf-8")),
            parser="screen_event_verify",
            source_meta={
                "screen_event_id": event_id,
                "occurred_at": ts,
                "trust_tier": e.get("trust_tier"),
                "focus_app": e.get("app"),
                "focus_title": e.get("window"),
                "time_range": _screen_event_time_range(e),
                "clip_path": _screen_event_clip_path(e),
            },
            chunks=[
                (
                    body,
                    "ambient",
                    {
                        "create_time": ts,
                        "screen_event_id": event_id,
                        "trust_tier": e.get("trust_tier"),
                        "app": e.get("app"),
                        "window": e.get("window"),
                    },
                )
            ],
            embeddings=vec,
        )
        conn.commit()
        return {"indexed": True, "query_vec": vec[0]}
    return {"indexed": False, "query_vec": np.ones(get_embed_dim(conn), dtype=np.float32)}


def _verify_index_one_temporal_screen_event(conn, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    for e in events:
        time_range = _screen_event_time_range(e)
        clip_path = _screen_event_clip_path(e)
        if not time_range and not clip_path:
            continue
        body = _event_search_document(e)
        dim = get_embed_dim(conn)
        vec = np.ones((1, dim), dtype=np.float32)
        ts = float(e.get("occurred_at") or time.time())
        event_id = str(e.get("event_id") or "verify-video")
        upsert_source(
            conn,
            path=f"ambient/screen-events/verify/{event_id}.md",
            kind="screen-event",
            sha256=_sha256_text(body),
            mtime=ts,
            bytes_=len(body.encode("utf-8")),
            parser="screen_event_verify",
            source_meta={
                "screen_event_id": event_id,
                "occurred_at": ts,
                "trust_tier": e.get("trust_tier"),
                "focus_app": e.get("app"),
                "focus_title": e.get("window"),
                "time_range": time_range,
                "clip_path": clip_path,
            },
            chunks=[
                (
                    body,
                    "ambient",
                    {
                        "create_time": ts,
                        "screen_event_id": event_id,
                        "trust_tier": e.get("trust_tier"),
                        "app": e.get("app"),
                        "window": e.get("window"),
                        "time_range": time_range,
                        "clip_path": clip_path,
                    },
                )
            ],
            embeddings=vec,
        )
        conn.commit()
        return {"indexed": True, "query_vec": vec[0]}
    return {"indexed": False, "query_vec": np.ones(get_embed_dim(conn), dtype=np.float32)}


def _write_verify_screenshot(data_dir: Path) -> Path:
    path = data_dir.parent / "inbox" / "screen-memory" / "verify.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw  # type: ignore

        im = Image.new("RGB", (420, 120), "white")
        draw = ImageDraw.Draw(im)
        draw.text((20, 35), "Export payout history", fill="black")
        im.save(path)
    except Exception:
        path.write_bytes(b"")
    return path


def _live_screen_probe(data_dir: Path) -> Dict[str, Any]:
    """Run lightweight local checks without storing sensitive content."""
    out = {"ran": True, "platform": os.uname().sysname if hasattr(os, "uname") else os.name}
    out["screencapture"] = _probe_screencapture(data_dir)
    out["rolling_video"] = _probe_rolling_video(data_dir)
    out["playwright_dom"] = _probe_playwright_dom(data_dir)
    out["adapter_commands"] = probe_screen_adapters(data_dir)
    out["clipboard"] = _probe_clipboard()
    out["frontmost_app"] = _probe_frontmost_app()
    return out


def _gate(gate_id: str, ok: Optional[bool], detail: str) -> Dict[str, Any]:
    if ok is True:
        status = "pass"
    elif ok is False:
        status = "blocked"
    else:
        status = "unknown"
    return {"id": gate_id, "ok": ok, "status": status, "detail": detail}


def _screen_memory_completion_gates(status: Dict[str, Any]) -> Dict[str, Any]:
    collectors = status.get("collectors") or {}
    recent = status.get("recent") or {}
    adapters = status.get("adapters") or {}
    probe = status.get("probe") or {}
    gates: List[Dict[str, Any]] = []

    gates.append(
        _gate(
            "voice_default_off",
            bool(status.get("voice_default_off")),
            "listening and full_listening default off",
        )
    )

    required_collectors = (
        "screen_reader",
        "screenshot_fallback",
        "dom_snapshot",
        "clipboard_event",
        "mouse_event",
        "keyboard_event",
        "rolling_video_clip",
    )
    disabled = [key for key in required_collectors if collectors.get(key) is False]
    gates.append(
        _gate(
            "collectors_enabled",
            not disabled,
            "all screen collectors enabled" if not disabled else "disabled: " + ", ".join(disabled),
        )
    )

    if probe.get("ran"):
        failed = [
            key
            for key in ("screencapture", "rolling_video", "playwright_dom", "clipboard", "frontmost_app")
            if not (probe.get(key) or {}).get("ok")
        ]
        gates.append(
            _gate(
                "live_capture_probe",
                not failed,
                "probe captured screen/video/dom/accessibility checks"
                if not failed
                else "failed: " + "; ".join(_probe_failure_summary(key, probe.get(key) or {}) for key in failed),
            )
        )
    else:
        gates.append(_gate("live_capture_probe", None, "run screen-memory-status --probe"))

    raw_count = int(recent.get("raw_event_count") or 0)
    gates.append(
        _gate(
            "recent_raw_capture",
            raw_count > 0,
            f"{raw_count} recent ambient rows",
        )
    )
    fused_count = int(recent.get("fused_event_count") or 0)
    gates.append(
        _gate(
            "recent_fused_events",
            fused_count > 0,
            f"{fused_count} recent fused screen events",
        )
    )
    clip_count = len(recent.get("video_clips") or [])
    gates.append(
        _gate(
            "recent_rolling_video_clips",
            clip_count > 0,
            f"{clip_count} recent rolling video clips",
        )
    )
    source_count = int(recent.get("recent_screen_event_sources") or 0)
    total_source_count = int(recent.get("screen_event_sources") or 0)
    gates.append(
        _gate(
            "indexed_screen_events",
            source_count > 0,
            f"{source_count} recent / {total_source_count} total indexed screen-event sources",
        )
    )
    graph_count = int(recent.get("open_graph_candidates") or 0)
    screen_graph_count = int(recent.get("recent_screen_graph_candidates") or 0)
    total_screen_graph_count = int(recent.get("screen_graph_candidates") or 0)
    gates.append(
        _gate(
            "graph_fill_pipeline",
            screen_graph_count > 0,
            (
                f"{graph_count} open / {screen_graph_count} recent / "
                f"{total_screen_graph_count} total graph candidates from screen evidence"
            ),
        )
    )

    gates.append(_adapter_gate("playwright_dom_adapter", "playwright_dom", adapters, probe))
    gates.append(_adapter_gate("marlin_adapter", "marlin", adapters, probe))
    gates.append(_adapter_gate("omniparser_adapter", "omniparser", adapters, probe))

    required_ids = {g["id"] for g in gates}
    passed = [g["id"] for g in gates if g.get("status") == "pass"]
    blocked = [g for g in gates if g.get("status") == "blocked"]
    unknown = [g for g in gates if g.get("status") == "unknown"]
    return {
        "overall_ready": not blocked and not unknown and len(passed) == len(required_ids),
        "passed": passed,
        "blocked": blocked,
        "unknown": unknown,
        "gates": gates,
    }


def _adapter_gate(
    gate_id: str,
    adapter_key: str,
    adapters: Dict[str, Any],
    probe: Dict[str, Any],
) -> Dict[str, Any]:
    config = adapters.get(adapter_key) or {}
    if not config.get("configured"):
        env = config.get("env_key") or (
            "MINION_MARLIN_CMD" if adapter_key == "marlin" else "MINION_OMNIPARSER_CMD"
        )
        if adapter_key == "playwright_dom":
            return _gate(gate_id, False, "built-in Playwright DOM disabled or script unavailable")
        return _gate(gate_id, False, f"{env} not configured")
    if not probe.get("ran"):
        return _gate(gate_id, None, "configured; run screen-memory-status --probe to validate")
    if adapter_key == "playwright_dom":
        p = probe.get("playwright_dom") or {}
    else:
        p = (probe.get("adapter_commands") or {}).get(adapter_key) or {}
    if not p:
        return _gate(gate_id, None, "probe did not return adapter result")
    return _gate(gate_id, bool(p.get("ok")), _probe_gate_detail(p))


def _probe_gate_detail(probe_result: Dict[str, Any]) -> str:
    ok = bool(probe_result.get("ok"))
    detail = (
        probe_result.get("reason")
        or probe_result.get("error")
        or probe_result.get("stderr")
        or ("probe ok" if ok else "probe failed")
    )
    return str(detail or "")[:240]


def _probe_failure_summary(key: str, result: Dict[str, Any]) -> str:
    hint = str(result.get("hint") or "").strip()
    if hint:
        return f"{key} ({hint})"
    detail = str(result.get("reason") or result.get("error") or result.get("stderr") or "").strip()
    if detail:
        return f"{key} ({detail[:160]})"
    return key


def _screen_capture_hint(stderr: str, *, video: bool = False) -> str:
    text = str(stderr or "").casefold()
    if (
        "could not create image from display" in text
        or "capture error" in text
        or "operation could not be completed" in text
        or "dispatch_source_create returned null" in text
    ):
        target = "rolling video" if video else "screenshot"
        return (
            f"macOS blocked {target} capture; grant Screen Recording to Minion/Terminal "
            "or run `minion screen-memory-permissions`, then retry screen-memory-status --probe"
        )
    return ""


def _probe_screencapture(data_dir: Path) -> Dict[str, Any]:
    exe = Path("/usr/sbin/screencapture")
    if not exe.exists():
        return {"ok": False, "available": False, "error": "missing /usr/sbin/screencapture"}
    probe_dir = data_dir / "ambient" / "probes"
    probe_dir.mkdir(parents=True, exist_ok=True)
    path = probe_dir / f"screen-probe-{int(time.time() * 1000)}.png"
    try:
        proc = subprocess.run(
            [str(exe), "-x", "-t", "png", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
        ok = proc.returncode == 0 and path.is_file() and path.stat().st_size > 0
        size = path.stat().st_size if path.is_file() else 0
        stderr = (proc.stderr or "")[:500]
        return {
            "ok": ok,
            "available": True,
            "bytes": size,
            "returncode": proc.returncode,
            "stderr": stderr,
            "hint": "" if ok else _screen_capture_hint(stderr),
        }
    except Exception as exc:
        return {"ok": False, "available": True, "error": str(exc)[:500]}
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _probe_rolling_video(data_dir: Path) -> Dict[str, Any]:
    exe = Path("/usr/sbin/screencapture")
    if not exe.exists():
        return {"ok": False, "available": False, "error": "missing /usr/sbin/screencapture"}
    probe_dir = data_dir / "ambient" / "probes"
    probe_dir.mkdir(parents=True, exist_ok=True)
    path = probe_dir / f"screen-video-probe-{int(time.time() * 1000)}.mov"
    try:
        proc = subprocess.run(
            [str(exe), "-x", "-m", "-v", "-V1", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=6,
        )
        ok = proc.returncode == 0 and path.is_file() and path.stat().st_size > 0
        size = path.stat().st_size if path.is_file() else 0
        stderr = (proc.stderr or "")[:500]
        return {
            "ok": ok,
            "available": True,
            "bytes": size,
            "duration_sec": 1,
            "returncode": proc.returncode,
            "stderr": stderr,
            "hint": "" if ok else _screen_capture_hint(stderr, video=True),
        }
    except Exception as exc:
        return {"ok": False, "available": True, "error": str(exc)[:500]}
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _probe_playwright_dom(data_dir: Path) -> Dict[str, Any]:
    script = Path(__file__).resolve().parents[2] / "desktop" / "scripts" / "playwright-dom-snapshot.mjs"
    if not script.is_file():
        return {"ok": False, "available": False, "error": "missing playwright DOM script"}
    url = "data:text/html,<html><title>MinionProbe</title><body><button>Export</button><p>Payouts</p></body></html>"
    try:
        proc = subprocess.run(
            ["node", str(script), url],
            cwd=str(Path(data_dir).expanduser().resolve()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        parsed: Dict[str, Any] = {}
        try:
            parsed = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            parsed = {}
        elements = parsed.get("visible_elements") if isinstance(parsed, dict) else []
        ok = proc.returncode == 0 and isinstance(elements, list) and any(
            str(e.get("label") or "").strip() == "Export"
            for e in elements
            if isinstance(e, dict)
        )
        return {
            "ok": ok,
            "available": True,
            "returncode": proc.returncode,
            "title": str(parsed.get("window_title") or "")[:120] if isinstance(parsed, dict) else "",
            "visible_elements": len(elements) if isinstance(elements, list) else 0,
            "stderr": (proc.stderr or "")[:500],
        }
    except Exception as exc:
        return {"ok": False, "available": True, "error": str(exc)[:500]}


def _probe_clipboard() -> Dict[str, Any]:
    exe = Path("/usr/bin/pbpaste")
    if not exe.exists():
        return {"ok": False, "available": False, "error": "missing /usr/bin/pbpaste"}
    try:
        proc = subprocess.run(
            [str(exe)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
        )
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        ok = proc.returncode == 0 or (proc.returncode == 1 and not stderr and not proc.stdout)
        return {
            "ok": ok,
            "available": True,
            "bytes": len(proc.stdout or b""),
            "returncode": proc.returncode,
            "stderr": stderr[:500],
            "content_captured": False,
        }
    except Exception as exc:
        return {"ok": False, "available": True, "error": str(exc)[:500], "content_captured": False}


def _probe_frontmost_app() -> Dict[str, Any]:
    exe = Path("/usr/bin/osascript")
    if not exe.exists():
        return {"ok": False, "available": False, "error": "missing /usr/bin/osascript"}
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    try:
        proc = subprocess.run(
            [str(exe), "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        app = (proc.stdout or "").strip()
        return {
            "ok": proc.returncode == 0 and bool(app),
            "available": True,
            "app_name": app[:200],
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "")[:500],
        }
    except Exception as exc:
        return {"ok": False, "available": True, "error": str(exc)[:500]}


def _screen_memory_readiness(status: Dict[str, Any]) -> Dict[str, Any]:
    collectors = status.get("collectors") or {}
    recent = status.get("recent") or {}
    adapters = status.get("adapters") or {}
    missing: List[str] = []
    warnings: List[str] = []
    for key in (
        "screen_reader",
        "screenshot_fallback",
        "dom_snapshot",
        "clipboard_event",
        "mouse_event",
        "keyboard_event",
        "rolling_video_clip",
    ):
        if collectors.get(key) is False:
            missing.append(f"collector_disabled:{key}")
    if not status.get("voice_default_off"):
        warnings.append("voice_or_full_listening_enabled")
    if int(recent.get("raw_event_count") or 0) == 0:
        warnings.append("no_recent_raw_ambient_events")
    if int(recent.get("fused_event_count") or 0) == 0:
        warnings.append("no_recent_fused_screen_events")
    if int(recent.get("recent_screen_event_sources") or 0) == 0:
        warnings.append("no_indexed_screen_event_sources")
    if not recent.get("video_clips"):
        warnings.append("no_recent_rolling_video_clips")
    if not (adapters.get("playwright_dom") or {}).get("configured"):
        warnings.append("playwright_dom_adapter_not_configured")
    if not (adapters.get("marlin") or {}).get("configured"):
        warnings.append("marlin_adapter_not_configured")
    if not (adapters.get("omniparser") or {}).get("configured"):
        warnings.append("omniparser_adapter_not_configured")
    probe = status.get("probe") or {}
    if probe.get("ran"):
        for key in ("screencapture", "rolling_video", "playwright_dom", "clipboard", "frontmost_app"):
            p = probe.get(key) or {}
            if not p.get("ok"):
                warnings.append(f"{key}_probe_failed")
                if (
                    key in ("screencapture", "rolling_video")
                    and p.get("hint")
                    and "screen_recording_permission_blocked" not in warnings
                ):
                    warnings.append("screen_recording_permission_blocked")
        adapters_probe = probe.get("adapter_commands") or {}
        for key in ("marlin", "omniparser"):
            p = adapters_probe.get(key) or {}
            if p.get("configured") and not p.get("ok"):
                warnings.append(f"{key}_adapter_probe_failed:{p.get('reason') or 'failed'}")
    ready = not missing and int(recent.get("fused_event_count") or 0) > 0
    return {
        "ready": ready,
        "missing": missing,
        "warnings": warnings,
        "summary": "screen memory has recent fused evidence" if ready else "screen memory needs live capture evidence or setup",
    }


def _task_title_from_summary(summary: Dict[str, Any]) -> str:
    windows = summary.get("recent_windows") or []
    if windows:
        first = windows[0]
        app = str(first.get("app") or "").strip()
        window = str(first.get("window") or "").strip()
        if app and window:
            return f"Follow up on {app}: {window}"[:500]
        if app:
            return f"Follow up on {app}"[:500]
    return "Follow up from recent screen work"


def _event_search_document(e: Dict[str, Any]) -> str:
    lines = [
        f"# Screen event: {e.get('app') or 'unknown'}",
        "",
        f"time: {e.get('occurred_at')}",
        f"app: {e.get('app') or ''}",
        f"window: {e.get('window') or ''}",
        f"url: {e.get('url') or ''}",
        f"trust_tier: {e.get('trust_tier') or ''}",
        f"confidence: {e.get('confidence') or ''}",
        "",
        str(e.get("scene") or ""),
    ]
    visible = e.get("visible_elements") or []
    if isinstance(visible, list) and visible:
        lines.extend(["", "Visible elements:"])
        for item in visible[:40]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("text") or "").strip()
            role = str(item.get("role") or "element").strip()
            source = str(item.get("source") or "").strip()
            if label or role:
                lines.append(f"- {role}: {label} ({source})".strip())
    actions = e.get("events") or []
    if isinstance(actions, list) and actions:
        lines.extend(["", "Events:"])
        for item in actions[:40]:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            kind = str(item.get("type") or "event").strip()
            source = str(item.get("source") or "").strip()
            when = str(item.get("time_range") or "").strip()
            if summary:
                suffix = f" @ {when}" if when else ""
                lines.append(f"- {kind}: {summary} ({source}){suffix}".strip())
    raw = e.get("raw") or {}
    if isinstance(raw, dict):
        payload = raw.get("payload") or {}
        if isinstance(payload, dict):
            extras = []
            for key in ("text_excerpt", "detected_emails", "clip_path", "source_path", "time_range", "start_sec", "end_sec"):
                if payload.get(key):
                    extras.append(f"{key}: {payload.get(key)}")
            if extras:
                lines.extend(["", "Evidence:", *extras])
    return "\n".join(lines).strip() + "\n"


def _screen_event_time_range(e: Dict[str, Any]) -> str:
    for action in e.get("events") or []:
        if isinstance(action, dict) and action.get("time_range"):
            return str(action.get("time_range") or "").strip()
    raw = e.get("raw") or {}
    payload = raw.get("payload") if isinstance(raw, dict) else {}
    if isinstance(payload, dict):
        tr = str(payload.get("time_range") or "").strip()
        if tr:
            return tr
        start = _first_num(payload, "start_sec", "start_seconds", "start_time", "timestamp_sec", "timestamp")
        end = _first_num(payload, "end_sec", "end_seconds", "end_time")
        if start is not None or end is not None:
            return _format_time_range(start, end)
    return ""


def _screen_event_clip_path(e: Dict[str, Any]) -> str:
    raw = e.get("raw") or {}
    payload = raw.get("payload") if isinstance(raw, dict) else {}
    if isinstance(payload, dict):
        return str(payload.get("clip_path") or payload.get("source_path") or "").strip()
    return ""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _open_screen_entity_keys(conn) -> set[str]:
    keys: set[str] = set()
    for c in graph_candidate_list(conn, status="open", limit=200):
        payload = c.get("payload") or {}
        key = str(payload.get("dedupe_key") or "").strip()
        if key:
            keys.add(key)
        if payload.get("entity_type") == "email" and payload.get("email"):
            keys.add(f"email:{str(payload['email']).strip().casefold()}")
        for ident in _entity_identifiers(payload):
            keys.add(ident)
    return keys


def _screen_entity_known_in_graph(conn, entity: Dict[str, Any]) -> bool:
    label = _norm_entity_label(entity.get("label") or entity.get("name") or "")
    identifiers = _entity_identifiers(entity)
    if not label and not identifiers:
        return True
    rows = conn.execute(
        "SELECT title, aliases_json, summary FROM graph_nodes WHERE node_kind='person' "
        "AND status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    for row in rows:
        if label and label == _norm_entity_label(row["title"] or ""):
            return True
        meta = _json_obj(row["summary"])
        row_ids = _entity_identifiers(meta)
        try:
            aliases = json.loads(row["aliases_json"] or "[]")
        except Exception:
            aliases = []
        for alias in aliases if isinstance(aliases, list) else []:
            row_ids.update(_entity_identifiers({"email": alias, "phone": alias, "handle": alias, "imessage": alias}))
            if label and label == _norm_entity_label(alias):
                return True
        if identifiers and row_ids.intersection(identifiers):
            return True
    return False


def _json_obj(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _screen_entities_from_event(e: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = ((e.get("raw") or {}).get("payload") or {}) if isinstance(e.get("raw"), dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    texts = _screen_entity_texts(e, payload)
    entities: List[Dict[str, Any]] = []
    seen: set[str] = set()

    name_hints = _name_hints_from_payload(payload)
    for email in _emails_from_screen_event(e):
        label = _name_near_email(texts, email) or name_hints.get(email) or ""
        entity = {
            "entity_type": "email",
            "email": email,
            "label": label,
            "dedupe_key": f"email:{email}",
        }
        _append_entity(entities, seen, entity)

    for phone in _phones_from_screen_event(e):
        label = _messages_window_label(e) or _name_near_phone(texts, phone) or ""
        entity = {
            "entity_type": "person_identifier",
            "phone": phone,
            "imessage": phone,
            "label": label,
            "dedupe_key": f"phone:{phone}",
        }
        _append_entity(entities, seen, entity)

    label = _messages_window_label(e)
    handle = _messages_handle_from_payload(payload)
    if label and handle:
        digits = re.sub(r"\D+", "", handle)
        handle_key = f"phone:{digits[-10:]}" if len(digits) >= 10 else f"handle:{handle.casefold()}"
        entity = {
            "entity_type": "person_identifier",
            "label": label,
            "handle": "" if len(digits) >= 10 else handle,
            "phone": digits[-10:] if len(digits) >= 10 else "",
            "imessage": digits[-10:] if len(digits) >= 10 else handle,
            "dedupe_key": handle_key,
        }
        _append_entity(entities, seen, entity)

    return entities


def _append_entity(out: List[Dict[str, Any]], seen: set[str], entity: Dict[str, Any]) -> None:
    key = str(entity.get("dedupe_key") or "").strip()
    if not key or key in seen:
        return
    seen.add(key)
    clean = {k: v for k, v in entity.items() if str(v or "").strip()}
    out.append(clean)


def _screen_entity_texts(e: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
    texts: List[str] = [str(e.get("scene") or ""), str(e.get("url") or ""), str(e.get("window") or "")]
    texts.extend(str(payload.get(k) or "") for k in ("text_excerpt", "summary", "dom_text_sample", "window_title"))
    for action in e.get("events") or []:
        if isinstance(action, dict):
            texts.append(str(action.get("summary") or ""))
    for item in e.get("visible_elements") or []:
        if isinstance(item, dict):
            texts.append(str(item.get("label") or item.get("text") or ""))
    return [t for t in texts if t.strip()]


def _name_hints_from_payload(payload: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key in ("contacts", "people", "recipients"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = _clean_person_label(row.get("display_name") or row.get("name") or row.get("label") or "")
            for email in row.get("emails") or [row.get("email")]:
                norm = _normalize_email(email)
                if norm and label:
                    out[norm] = label
    for email in _coerce_list(payload.get("detected_emails")):
        norm = _normalize_email(email)
        label = _clean_person_label(payload.get("display_name") or payload.get("contact_name") or payload.get("name") or "")
        if norm and label:
            out[norm] = label
    return out


def _emails_from_screen_event(e: Dict[str, Any]) -> List[str]:
    seen: set[str] = set()
    payload = ((e.get("raw") or {}).get("payload") or {}) if isinstance(e.get("raw"), dict) else {}
    raw_emails = payload.get("detected_emails") if isinstance(payload, dict) else None
    if isinstance(raw_emails, list):
        for item in raw_emails:
            _add_email(seen, item)
    texts: List[str] = [str(e.get("scene") or ""), str(e.get("url") or "")]
    if isinstance(payload, dict):
        texts.extend(str(payload.get(k) or "") for k in ("text_excerpt", "summary", "dom_text_sample"))
    for action in e.get("events") or []:
        if isinstance(action, dict):
            texts.append(str(action.get("summary") or ""))
    for text in texts:
        for match in re.findall(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", text):
            _add_email(seen, match)
    return sorted(seen)


def _phones_from_screen_event(e: Dict[str, Any]) -> List[str]:
    seen: set[str] = set()
    payload = ((e.get("raw") or {}).get("payload") or {}) if isinstance(e.get("raw"), dict) else {}
    if isinstance(payload, dict):
        for key in ("detected_phones", "phones"):
            for item in _coerce_list(payload.get(key)):
                _add_phone(seen, item)
        for key in ("phone", "imessage", "handle"):
            _add_phone(seen, payload.get(key))
    texts = _screen_entity_texts(e, payload if isinstance(payload, dict) else {})
    for text in texts:
        for match in re.findall(r"(?:\+?1[\s.\-()]*)?(?:\(?\d{3}\)?[\s.\-]*)\d{3}[\s.\-]*\d{4}", text):
            _add_phone(seen, match)
    return sorted(seen)


def _add_email(out: set[str], raw: Any) -> None:
    email = _normalize_email(raw)
    if email:
        out.add(email)


def _normalize_email(raw: Any) -> str:
    email = str(raw or "").strip().strip("<>,.;:()[]{}\"'").casefold()
    return email if re.fullmatch(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", email) else ""


def _add_phone(out: set[str], raw: Any) -> None:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if len(digits) >= 10:
        out.add(digits[-10:])


def _coerce_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value:
        return [value]
    return []


def _messages_window_label(e: Dict[str, Any]) -> str:
    app = str(e.get("app") or "").casefold()
    if "message" not in app and "imessage" not in app:
        return ""
    title = _clean_person_label(e.get("window") or "")
    if not title or "message" in title.casefold():
        return ""
    return title


def _messages_handle_from_payload(payload: Dict[str, Any]) -> str:
    for key in ("handle", "imessage", "sender", "recipient"):
        raw = str(payload.get(key) or "").strip()
        if not raw:
            continue
        email = _normalize_email(raw)
        if email:
            return email
        digits = re.sub(r"\D+", "", raw)
        if len(digits) >= 10:
            return digits[-10:]
        if re.fullmatch(r"@[A-Za-z0-9_.-]{2,64}", raw):
            return raw.casefold()
    return ""


def _name_near_email(texts: List[str], email: str) -> str:
    escaped = re.escape(email)
    for text in texts:
        for pat in (
            rf"([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){{0,3}})\s*<\s*{escaped}\s*>",
            rf"(?:name|contact|from|to)\s*[:\-]\s*([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){{0,3}}).*?{escaped}",
        ):
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                label = _clean_person_label(m.group(1))
                if label:
                    return label
    return ""


def _name_near_phone(texts: List[str], phone: str) -> str:
    for text in texts:
        if phone not in re.sub(r"\D+", "", text):
            continue
        m = re.search(r"(?:name|contact|from|to)\s*[:\-]\s*([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3})", text)
        if m:
            label = _clean_person_label(m.group(1))
            if label:
                return label
    return ""


def _clean_person_label(raw: Any) -> str:
    label = re.sub(r"\s+", " ", str(raw or "").strip().strip("()[]{}<>\"'"))
    label = re.sub(
        r"^(?:user\s+)?(?:copied|copying|messaged|emailed|sent|received|from|to)\s+",
        "",
        label,
        flags=re.IGNORECASE,
    ).strip()
    if not label or len(label) > 80:
        return ""
    if re.search(r"@|https?://|\d{4,}", label):
        return ""
    words = label.split()
    if len(words) > 5:
        return ""
    return label


def _norm_entity_label(raw: Any) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip()).casefold()


def _entity_identifiers(entity: Dict[str, Any]) -> set[str]:
    out: set[str] = set()
    email = _normalize_email(entity.get("email"))
    if email:
        out.add(f"email:{email}")
    for key in ("phone", "imessage"):
        digits = re.sub(r"\D+", "", str(entity.get(key) or ""))
        if len(digits) >= 10:
            out.add(f"phone:{digits[-10:]}")
    handle = str(entity.get("handle") or "").strip().casefold()
    if handle:
        out.add(f"handle:{handle}")
    return out


def _screen_entity_title(entity: Dict[str, Any]) -> str:
    label = _clean_person_label(entity.get("label") or "")
    if label:
        return f"Who is {label}?"
    if entity.get("email"):
        return f"Who is {entity['email']}?"
    if entity.get("phone"):
        return f"Who is {entity['phone']}?"
    return "Who is this person?"


def _screen_entity_descriptor(entity: Dict[str, Any]) -> str:
    label = _clean_person_label(entity.get("label") or "")
    email = str(entity.get("email") or "").strip()
    phone = str(entity.get("phone") or "").strip()
    handle = str(entity.get("handle") or "").strip()
    if label and email:
        return f"`{label}` (`{email}`)"
    if label and (phone or handle):
        return f"`{label}` (`{phone or handle}`)"
    if email:
        return f"`{email}`"
    if phone:
        return f"`{phone}`"
    if handle:
        return f"`{handle}`"
    return "`a person mention`"


def _screen_event_refs(e: Dict[str, Any]) -> List[str]:
    refs = [f"screen_event:{e.get('event_id')}"] if e.get("event_id") else []
    for ref in e.get("source_refs") or []:
        s = str(ref or "").strip()
        if s and s not in refs:
            refs.append(s)
    return refs[:8]


def _infer_query_time_window(query: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
    q = query.casefold()
    now = time.time()
    lt = time.localtime(now)
    today_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    if "yesterday" in q:
        return today_start - 86400, today_start, "yesterday"
    if "today" in q:
        return today_start, None, "today"
    m = re.search(r"\blast\s+(\d+)\s*(minute|minutes|min|m|hour|hours|h)\b", q)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        seconds = n * 3600 if unit.startswith("h") or unit.startswith("hour") else n * 60
        return now - seconds, None, f"last {n} {unit}"
    return None, None, None


def _context_key(app: str, window: str) -> tuple[str, str]:
    return (str(app or "").strip().casefold(), str(window or "").strip().casefold())


def _remember_screen_context(contexts: Dict[tuple[str, str], Dict[str, Any]], fused: Dict[str, Any]) -> None:
    elements = fused.get("visible_elements") or []
    if not elements:
        return
    ctx = {
        "event_id": fused.get("event_id"),
        "app": fused.get("app"),
        "window": fused.get("window"),
        "url": fused.get("url"),
        "scene": fused.get("scene"),
        "visible_elements": elements,
        "trust_tier": fused.get("trust_tier"),
    }
    app = str(fused.get("app") or "")
    window = str(fused.get("window") or "")
    contexts[_context_key(app, window)] = ctx
    contexts[_context_key(app, "")] = ctx


def _matching_screen_context(
    contexts: Dict[tuple[str, str], Dict[str, Any]],
    fused: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    app = str(fused.get("app") or "")
    window = str(fused.get("window") or "")
    return contexts.get(_context_key(app, window)) or contexts.get(_context_key(app, ""))


def _merge_user_event_context(fused: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    payload = ((fused.get("raw") or {}).get("payload") or {}) if isinstance(fused.get("raw"), dict) else {}
    target = _target_element_for_payload(payload, ctx.get("visible_elements") or [])
    if target:
        fused["visible_elements"] = [target]
        label = str(target.get("label") or "").strip()
        role = str(target.get("role") or "element").strip()
        source = str(target.get("source") or ctx.get("trust_tier") or "context").strip()
        for action in fused.get("events") or []:
            if not isinstance(action, dict):
                continue
            old = str(action.get("summary") or "").strip()
            if label:
                action["summary"] = f"{old} Nearby UI target: {role} '{label}'."[:500]
            action["source"] = f"{action.get('source') or 'user_event'} + {source}"
            action["target"] = target
    else:
        fused["visible_elements"] = list(ctx.get("visible_elements") or [])[:8]
    if ctx.get("url") and not fused.get("url"):
        fused["url"] = ctx.get("url")
    refs = [str(x) for x in (fused.get("source_refs") or []) if x]
    ctx_ref = str(ctx.get("event_id") or "")
    if ctx_ref and ctx_ref not in refs:
        refs.append(ctx_ref)
    fused["source_refs"] = refs


def _target_element_for_payload(payload: Dict[str, Any], elements: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not elements:
        return None
    click = payload.get("last_click") or {}
    if isinstance(click, dict):
        x = _num(click.get("x"))
        y = _num(click.get("y"))
        if x is not None and y is not None:
            hit = _element_containing_point(elements, x, y)
            if hit:
                return hit
            nearest = _nearest_element(elements, x, y)
            if nearest:
                return nearest
    for e in elements:
        role = str(e.get("role") or "").casefold()
        label = str(e.get("label") or "").strip()
        if label and any(k in role for k in ("button", "link", "menu", "tab")):
            return e
    return next((e for e in elements if str(e.get("label") or "").strip()), elements[0])


def _element_containing_point(elements: List[Dict[str, Any]], x: float, y: float) -> Optional[Dict[str, Any]]:
    for e in elements:
        rect = _rect(e.get("bounds"))
        if not rect:
            continue
        left, top, width, height = rect
        if left <= x <= left + width and top <= y <= top + height:
            return e
    return None


def _nearest_element(elements: List[Dict[str, Any]], x: float, y: float) -> Optional[Dict[str, Any]]:
    best: tuple[float, Dict[str, Any]] | None = None
    for e in elements:
        rect = _rect(e.get("bounds"))
        if not rect:
            continue
        left, top, width, height = rect
        cx = left + width / 2.0
        cy = top + height / 2.0
        dist = (cx - x) ** 2 + (cy - y) ** 2
        if best is None or dist < best[0]:
            best = (dist, e)
    return best[1] if best else None


def _rect(raw: Any) -> Optional[tuple[float, float, float, float]]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        vals = [_num(v) for v in raw[:4]]
        if all(v is not None for v in vals):
            return (float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3]))
    if isinstance(raw, dict):
        vals = [_num(raw.get(k)) for k in ("x", "y", "width", "height")]
        if all(v is not None for v in vals):
            return (float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3]))
    return None


def _num(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _fuse_ambient_event(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = e.get("payload") or {}
    event_type = str(e.get("event_type") or "")
    event_id = f"screen-{e.get('event_id')}"
    ts = float(e.get("captured_at") or time.time())
    app = str(payload.get("app_name") or payload.get("app") or "").strip()
    window = str(payload.get("window_title") or payload.get("window") or "").strip()
    url = payload.get("url") or payload.get("url_or_host")
    visible_elements: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []
    scene = ""
    confidence = 0.4
    trust_tier = "ocr"

    if event_type in ("window_snapshot", "ax_content_changed", "window_focus", "browser_visit", "dom_snapshot"):
        trust_tier = "dom_or_accessibility" if event_type in ("window_snapshot", "ax_content_changed", "dom_snapshot") else "metadata"
        confidence = 0.92 if trust_tier == "dom_or_accessibility" else 0.65
        scene = _scene_from_window(app, window, payload)
        visible_elements = _visible_elements_from_payload(payload, event_type)
    elif event_type in ("mouse_event", "keyboard_event", "clipboard_event"):
        trust_tier = "user_events"
        confidence = 0.96
        scene = _scene_from_window(app, window, payload)
        actions = [_action_from_payload(payload, event_type)]
    elif event_type in ("rolling_video_clip", "marlin_event"):
        trust_tier = "temporal_video_events"
        confidence = float(payload.get("confidence") or (0.6 if event_type == "rolling_video_clip" else 0.78))
        scene = str(payload.get("scene") or payload.get("caption") or payload.get("summary") or "").strip()
        if not scene and event_type == "rolling_video_clip":
            scene = _clip_scene_from_payload(app, window, payload)
        actions = _events_from_payload(payload, source="clip" if event_type == "rolling_video_clip" else "marlin")
    elif event_type == "omniparser_parse":
        trust_tier = "visual_ui_parser"
        confidence = float(payload.get("confidence") or 0.74)
        scene = _scene_from_window(app, window, payload)
        visible_elements = _visible_elements_from_payload(payload, event_type)
    elif event_type == "screenshot_fallback":
        trust_tier = "ocr"
        confidence = 0.55
        scene = _scene_from_window(app, window, payload)
    elif event_type in ("general_vlm", "vlm_reasoning", "vlm_parse"):
        trust_tier = "general_vlm"
        confidence = float(payload.get("confidence") or 0.35)
        scene = str(payload.get("scene") or payload.get("caption") or payload.get("summary") or "").strip()
    else:
        return None

    if not scene:
        scene = _scene_from_window(app, window, payload)
    return {
        "event_id": event_id,
        "occurred_at": ts,
        "app": app,
        "window": window,
        "url": str(url) if url else None,
        "scene": scene,
        "visible_elements": visible_elements,
        "events": actions,
        "source_refs": [str(e.get("event_id") or "")],
        "confidence": confidence,
        "trust_tier": trust_tier,
        "raw": {"ambient_event_type": event_type, "payload": payload},
    }


def _scene_from_window(app: str, window: str, payload: Dict[str, Any]) -> str:
    explicit = payload.get("scene") or payload.get("summary")
    if explicit:
        return str(explicit).strip()[:1000]
    target = window or str(payload.get("url") or payload.get("url_or_host") or "").strip()
    if app and target:
        return f"User is working in {app}: {target}."
    if app:
        return f"User is working in {app}."
    if target:
        return f"User is viewing {target}."
    return ""


def _clip_scene_from_payload(app: str, window: str, payload: Dict[str, Any]) -> str:
    duration = payload.get("duration_sec")
    target = window or str(payload.get("clip_path") or "").strip()
    if app and target:
        return f"Recorded a {duration or 'short'} second screen clip in {app}: {target}."
    if app:
        return f"Recorded a {duration or 'short'} second screen clip in {app}."
    return f"Recorded a {duration or 'short'} second screen clip."


def _visible_elements_from_payload(payload: Dict[str, Any], event_type: str) -> List[Dict[str, Any]]:
    raw = payload.get("visible_elements") or payload.get("elements")
    if isinstance(raw, list):
        return [_clean_element(x, source=event_type) for x in raw if isinstance(x, dict)][:80]
    out: List[Dict[str, Any]] = []
    nodes = payload.get("ax_nodes")
    if isinstance(nodes, list):
        for n in nodes[:80]:
            if not isinstance(n, dict):
                continue
            label = n.get("label") or n.get("title") or n.get("value") or n.get("description") or n.get("name")
            role = n.get("role") or "element"
            if not label and not role:
                continue
            out.append(
                {
                    "role": str(role),
                    "label": str(label or "")[:300],
                    "bounds": n.get("bounds") or n.get("frame"),
                    "source": "AX",
                    "confidence": 0.92,
                }
            )
    return out


def _clean_element(x: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    return {
        "role": str(x.get("role") or x.get("type") or "element")[:80],
        "label": str(x.get("label") or x.get("text") or x.get("caption") or "")[:300],
        "bounds": x.get("bounds") or x.get("bbox"),
        "source": str(x.get("source") or source),
        "confidence": float(x.get("confidence") or 0.7),
    }


def _action_from_payload(payload: Dict[str, Any], event_type: str) -> Dict[str, Any]:
    summary = payload.get("summary")
    if not summary:
        if event_type == "clipboard_event":
            summary = "User copied or changed clipboard content."
        elif event_type == "mouse_event":
            summary = "User used the mouse."
        else:
            summary = "User used the keyboard."
    return {
        "type": "user_action",
        "summary": str(summary)[:500],
        "source": event_type,
        "confidence": 0.96,
    }


def _events_from_payload(payload: Dict[str, Any], *, source: str) -> List[Dict[str, Any]]:
    raw = payload.get("events")
    if isinstance(raw, list):
        out = []
        for item in raw[:40]:
            if isinstance(item, dict):
                event = {
                    "type": str(item.get("type") or "scene_event")[:80],
                    "summary": str(item.get("summary") or item.get("caption") or "")[:500],
                    "source": source,
                    "confidence": float(item.get("confidence") or payload.get("confidence") or 0.78),
                }
                _attach_temporal_fields(event, item)
                out.append(
                    event
                )
        return out
    if source == "marlin":
        summary = str(payload.get("scene") or payload.get("summary") or payload.get("caption") or "").strip()
        if summary:
            event = {
                "type": "scene_event",
                "summary": summary[:500],
                "source": source,
                "confidence": float(payload.get("confidence") or 0.78),
            }
            _attach_temporal_fields(event, payload)
            return [event]
    return []


def _attach_temporal_fields(event: Dict[str, Any], source: Dict[str, Any]) -> None:
    start = _first_num(
        source,
        "start_sec",
        "start_seconds",
        "start_time",
        "timestamp_sec",
        "timestamp",
    )
    end = _first_num(source, "end_sec", "end_seconds", "end_time")
    duration = _first_num(source, "duration_sec", "duration")
    if end is None and start is not None and duration is not None:
        end = start + duration
    if start is not None:
        event["start_sec"] = start
    if end is not None:
        event["end_sec"] = end
    tr = str(source.get("time_range") or "").strip()
    if not tr and (start is not None or end is not None):
        tr = _format_time_range(start, end)
    if tr:
        event["time_range"] = tr


def _first_num(source: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key in source:
            val = _num(source.get(key))
            if val is not None:
                return val
    return None


def _format_time_range(start: Optional[float], end: Optional[float]) -> str:
    if start is not None and end is not None:
        return f"{start:g}s-{end:g}s"
    if start is not None:
        return f"{start:g}s"
    return f"-{end:g}s" if end is not None else ""


def _ingest_screenshot_fallbacks(conn, data_dir: Path) -> Dict[str, Any]:
    since = time.time() - 6 * 3600.0
    events = ambient_events_since(conn, since_ts=since, limit=300)
    inbox = data_dir.parent / "inbox"
    attempted = indexed = skipped = 0
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for e in events:
        if e.get("event_type") != "screenshot_fallback":
            continue
        payload = e.get("payload") or {}
        rel = str(payload.get("screenshot_inbox_rel") or "").strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        path = (inbox / rel).resolve()
        attempted += 1
        if not path.is_file():
            skipped += 1
            items.append({"path": str(path), "skipped": True, "reason": "missing"})
            continue
        res = ingest_file(conn, path, force=False)
        if res.skipped:
            skipped += 1
        else:
            indexed += 1
        items.append(
            {
                "path": str(path),
                "source_id": res.source_id,
                "kind": res.kind,
                "parser": res.parser,
                "chunks": res.chunk_count,
                "skipped": res.skipped,
                "reason": res.reason,
            }
        )
    return {"attempted": attempted, "indexed": indexed, "skipped": skipped, "items": items[:20]}


def as_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
