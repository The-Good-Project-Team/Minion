"""Context Core — redundant capture, evidence fusion, candidate distillation, context bundle."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from settings import DEFAULT_AMBIENT_COLLECTORS, ambient_collector_enabled, load_settings
from store import ambient_events_since, graph_candidate_list, graph_scaffold_list

log = logging.getLogger(__name__)

# Collector catalog: settings key -> raw stream kinds / aliases
COLLECTOR_CATALOG: Dict[str, Dict[str, Any]] = {
    "window_focus": {
        "label": "Focused app/window",
        "kinds": ["window_focus", "window_snapshot"],
        "role": "focus",
    },
    "ax_content_changed": {
        "label": "Accessibility text",
        "kinds": ["ax_content_changed", "window_snapshot"],
        "role": "ax",
    },
    "screen_reader": {
        "label": "Screen reader / AX pipeline",
        "kinds": ["window_snapshot", "ax_content_changed"],
        "role": "ax",
    },
    "browser_visit": {
        "label": "Browser URL/title",
        "kinds": ["browser_visit"],
        "role": "browser",
    },
    "dom_snapshot": {
        "label": "Browser DOM text",
        "kinds": ["dom_snapshot"],
        "role": "browser",
    },
    "screenshot_fallback": {
        "label": "Screenshot OCR fallback",
        "kinds": ["screenshot_fallback"],
        "role": "ocr",
    },
    "clipboard_event": {
        "label": "Clipboard",
        "kinds": ["clipboard_event"],
        "role": "clipboard",
    },
    "app_launched": {
        "label": "App launch",
        "kinds": ["app_launched"],
        "role": "lifecycle",
    },
    "process_snapshot": {
        "label": "Process snapshot",
        "kinds": ["process_snapshot"],
        "role": "process",
    },
    "mouse_event": {"label": "Mouse", "kinds": ["mouse_event"], "role": "input"},
    "keyboard_event": {"label": "Keyboard", "kinds": ["keyboard_event"], "role": "input"},
    "rolling_video_clip": {
        "label": "Rolling video",
        "kinds": ["rolling_video_clip", "marlin_event"],
        "role": "video",
    },
    "listening": {"label": "Voice listening", "kinds": ["listening"], "role": "voice"},
    "full_listening": {"label": "Full listening", "kinds": ["full_listening"], "role": "voice"},
}

WITNESS_CONFIDENCE_BOOST: Dict[str, float] = {
    "window_snapshot": 0.12,
    "ax_content_changed": 0.14,
    "window_focus": 0.06,
    "browser_visit": 0.10,
    "dom_snapshot": 0.14,
    "screenshot_fallback": 0.08,
    "clipboard_event": 0.12,
    "app_launched": 0.04,
    "process_snapshot": 0.03,
    "mouse_event": 0.02,
    "keyboard_event": 0.02,
}

WITNESS_BUCKET_SEC = 45.0
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)


def ambient_coverage_status(
    conn,
    data_dir: Path,
    *,
    minutes: int = 60,
) -> Dict[str, Any]:
    """Per-collector enabled state, recent counts, gaps, and witness redundancy."""
    data = Path(data_dir).expanduser().resolve()
    mins = max(1, min(int(minutes), 24 * 60))
    since = time.time() - mins * 60.0
    settings = load_settings(data)
    rows = ambient_events_since(conn, since_ts=since, limit=2000)

    kind_counts: Dict[str, int] = defaultdict(int)
    kind_last_ts: Dict[str, float] = {}
    for row in rows:
        kind = str(row.get("event_type") or "unknown")
        kind_counts[kind] += 1
        ts = float(row.get("captured_at") or 0)
        if ts >= kind_last_ts.get(kind, 0):
            kind_last_ts[kind] = ts

    collectors_out: List[Dict[str, Any]] = []
    blind: List[str] = []
    for key, spec in COLLECTOR_CATALOG.items():
        enabled = ambient_collector_enabled(settings, key)
        kinds = spec.get("kinds") or [key]
        recent = sum(kind_counts.get(k, 0) for k in kinds)
        last_ts = max((kind_last_ts.get(k, 0) for k in kinds), default=0.0)
        status = "blind" if enabled and recent == 0 else ("ok" if recent else "disabled")
        if enabled and recent == 0:
            blind.append(key)
        collectors_out.append(
            {
                "collector": key,
                "label": spec.get("label") or key,
                "enabled": enabled,
                "recent_count": recent,
                "last_seen_at": last_ts if last_ts else None,
                "status": status,
                "stream_kinds": kinds,
            }
        )

    groups = group_ambient_witnesses(rows)
    multi_witness = sum(1 for g in groups if len(g) >= 2)
    inbox_sources = 0
    try:
        from store import count_sources

        inbox_sources = count_sources(conn)
    except Exception:
        pass

    return {
        "minutes": mins,
        "ambient_sensing_enabled": bool(settings.get("ambient_sensing_enabled", True)),
        "stream_exists": (data / "ambient" / "stream.jsonl").is_file(),
        "raw_event_count": len(rows),
        "raw_by_kind": dict(kind_counts),
        "collectors": collectors_out,
        "blind_collectors": blind,
        "witness_groups": len(groups),
        "multi_witness_groups": multi_witness,
        "indexed_source_count": inbox_sources,
        "generated_at": time.time(),
    }


def _witness_category(event_type: str) -> str:
    if event_type in ("mouse_event", "keyboard_event", "clipboard_event"):
        return "input"
    if event_type in ("rolling_video_clip", "marlin_event"):
        return "video"
    return "content"


def group_ambient_witnesses(rows: List[Dict[str, Any]], *, bucket_sec: float = WITNESS_BUCKET_SEC) -> List[List[Dict[str, Any]]]:
    """Group raw ambient rows that likely describe the same moment (app/window/time bucket)."""
    buckets: Dict[Tuple[str, str, int, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        app = str(payload.get("app_name") or payload.get("app") or "").strip().casefold()
        window = str(payload.get("window_title") or payload.get("window") or "").strip().casefold()[:120]
        ts = float(row.get("captured_at") or 0)
        slot = int(ts // max(1.0, bucket_sec))
        et = str(row.get("event_type") or "")
        cat = _witness_category(et)
        # Input witnesses stay separate; content/video merge within the time bucket.
        subtype = et if cat == "input" else ""
        buckets[(app, window, slot, cat, subtype)].append(row)
    return list(buckets.values())


def witness_confidence(collector_types: Set[str], base: float = 0.35) -> float:
    score = base
    for kind in collector_types:
        score += WITNESS_CONFIDENCE_BOOST.get(kind, 0.04)
    return min(score, 0.98)


def merge_witness_group(group: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Fuse a witness group into one semantic evidence dict (before screen_memory upsert)."""
    if not group:
        return None
    from screen_memory import _fuse_ambient_event

    priority = (
        "dom_snapshot",
        "ax_content_changed",
        "window_snapshot",
        "browser_visit",
        "marlin_event",
        "rolling_video_clip",
        "omniparser_parse",
        "screenshot_fallback",
        "clipboard_event",
    )
    by_type: Dict[str, Dict[str, Any]] = {}
    for row in group:
        et = str(row.get("event_type") or "")
        by_type[et] = row

    primary_row = None
    for pref in priority:
        if pref in by_type:
            primary_row = by_type[pref]
            break
    if primary_row is None:
        primary_row = group[0]

    fused = _fuse_ambient_event(primary_row)
    if not fused:
        return None

    collector_types = {str(r.get("event_type") or "") for r in group if r.get("event_type")}
    source_refs: List[str] = []
    for r in group:
        rid = str(r.get("event_id") or "").strip()
        if rid and rid not in source_refs:
            source_refs.append(rid)
    fused["source_refs"] = source_refs
    base_conf = float(fused.get("confidence") or 0.4)
    if len(group) > 1:
        fused["confidence"] = witness_confidence(collector_types, base_conf)
    else:
        fused["confidence"] = base_conf
    raw = dict(fused.get("raw") or {})
    raw["witness_count"] = len(group)
    raw["collector_types"] = sorted(collector_types)
    raw["time_window_sec"] = WITNESS_BUCKET_SEC
    fused["raw"] = raw
    return fused


def fuse_screen_events_with_witnesses(
    conn,
    *,
    since_hours: float = 6.0,
    limit: int = 500,
) -> Dict[str, Any]:
    """Normalize ambient stream into semantic events; redundant witnesses merge with boosted confidence."""
    from screen_memory import (
        _matching_screen_context,
        _merge_user_event_context,
        _remember_screen_context,
        screen_memory_event_upsert,
    )

    since = time.time() - max(0.1, float(since_hours)) * 3600.0
    rows = ambient_events_since(conn, since_ts=since, limit=limit)
    groups = group_ambient_witnesses(rows)
    upserted = 0
    by_trust: Dict[str, int] = {}
    contexts: Dict[tuple[str, str], Dict[str, Any]] = {}
    max_witness = 0

    for group in sorted(groups, key=lambda g: min(float(r.get("captured_at") or 0) for r in g)):
        fused = merge_witness_group(group)
        if not fused:
            continue
        raw = fused.get("raw") or {}
        raw_type = raw.get("ambient_event_type") if isinstance(raw, dict) else ""
        if fused.get("trust_tier") == "user_events" and raw_type in ("mouse_event", "keyboard_event"):
            ctx = _matching_screen_context(contexts, fused)
            if ctx:
                _merge_user_event_context(fused, ctx)
        screen_memory_event_upsert(conn, **fused)
        upserted += 1
        wc = int((raw or {}).get("witness_count") or 1)
        max_witness = max(max_witness, wc)
        tier = str(fused.get("trust_tier") or "unknown")
        by_trust[tier] = by_trust.get(tier, 0) + 1
        _remember_screen_context(contexts, fused)

    return {
        "upserted": upserted,
        "scanned": len(rows),
        "witness_groups": len(groups),
        "max_witness_count": max_witness,
        "by_trust": by_trust,
    }


def distill_evidence_candidates(
    conn,
    *,
    since_hours: float = 6.0,
    limit: int = 200,
    min_confidence: float = 0.72,
    min_witnesses: int = 1,
) -> Dict[str, Any]:
    """Turn high-confidence fused screen evidence into graph_candidates (never durable graph writes)."""
    from screen_memory import (
        _clean_person_label,
        _open_screen_entity_keys,
        _screen_entities_from_event,
        _screen_entity_descriptor,
        _screen_entity_known_in_graph,
        _screen_entity_title,
        _screen_event_refs,
    )
    from graph_fill import _is_plausible_graph_title
    from store import graph_candidate_create, screen_memory_events_since

    since = time.time() - max(0.1, float(since_hours)) * 3600.0
    rows = screen_memory_events_since(conn, since_ts=since, limit=limit)
    existing = _open_screen_entity_keys(conn)
    created = 0
    skipped = 0

    for e in rows:
        conf = float(e.get("confidence") or 0)
        raw = e.get("raw") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        witness_count = int((raw or {}).get("witness_count") or 1)
        if conf < min_confidence or witness_count < min_witnesses:
            skipped += 1
            continue
        collector_types = (raw or {}).get("collector_types") or []
        for entity in _screen_entities_from_event(e):
            key = str(entity.get("dedupe_key") or "").strip()
            if not key:
                continue
            if key in existing or _screen_entity_known_in_graph(conn, entity):
                skipped += 1
                continue
            label = _clean_person_label(entity.get("label") or "")
            if label and not _is_plausible_graph_title(label, node_kind="person"):
                skipped += 1
                continue
            app = str(e.get("app") or "").strip()
            window = str(e.get("window") or "").strip()
            where = f"{app}: {window}" if app and window else (app or "screen memory")
            graph_candidate_create(
                conn,
                candidate_type="evidence_person",
                title=_screen_entity_title(entity),
                body_md=(
                    f"Evidence from {where} ({witness_count} witness(es), "
                    f"collectors: {', '.join(collector_types[:6]) or 'ambient'}).\n\n"
                    f"Descriptor: {_screen_entity_descriptor(entity)}"
                ),
                payload={
                    **entity,
                    "witness_count": witness_count,
                    "collector_types": collector_types,
                    "confidence": conf,
                    "trust_tier": e.get("trust_tier"),
                    "screen_event_id": e.get("event_id"),
                    "import_policy": "candidates_only",
                },
                evidence_refs=_screen_event_refs(e),
                confidence=conf,
                source="context_core",
            )
            existing.add(key)
            created += 1

    return {"created": created, "skipped": skipped, "scanned_events": len(rows)}


def context_bundle(
    conn,
    data_dir: Path,
    *,
    subject: str = "",
    now: Optional[float] = None,
    memory_top_k: int = 4,
    for_mcp: bool = True,
) -> Dict[str, Any]:
    """Single context contract: graph, evidence, candidates, retrieval, focus."""
    ts = now if now is not None else time.time()
    subject = (subject or "").strip()
    why: List[str] = []

    coverage = ambient_coverage_status(conn, data_dir, minutes=60)
    if coverage.get("blind_collectors"):
        why.append(f"blind collectors: {', '.join(coverage['blind_collectors'][:5])}")

    graph_ctx: Dict[str, Any] = {}
    spine_md = ""
    try:
        from graph_context import build_graph_context
        from graph_ambient import build_graph_spine

        graph_ctx = build_graph_context(conn, data_dir, subject=subject, max_candidates=5)
        spine_md = (build_graph_spine(conn, data_dir).get("spine_md") or "")[:1400]
        why.append("durable graph spine attached")
    except Exception:
        graph_ctx = {}

    candidates = graph_candidate_list(conn, status="open", limit=8)
    high_conf = [c for c in candidates if float(c.get("confidence") or 0) >= 0.65][:5]
    if high_conf:
        why.append(f"{len(high_conf)} high-confidence open candidates")

    recent_evidence: List[Dict[str, Any]] = []
    try:
        from store import screen_memory_events_since

        for e in screen_memory_events_since(conn, since_ts=ts - 3600, limit=12):
            raw = e.get("raw") or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    raw = {}
            recent_evidence.append(
                {
                    "event_id": e.get("event_id"),
                    "app": e.get("app"),
                    "window": (e.get("window") or "")[:80],
                    "scene": (e.get("scene") or "")[:160],
                    "confidence": e.get("confidence"),
                    "witness_count": (raw or {}).get("witness_count"),
                    "collector_types": (raw or {}).get("collector_types"),
                }
            )
        if recent_evidence:
            why.append(f"{len(recent_evidence)} recent semantic evidence events")
    except Exception:
        pass

    related_memory: List[Dict[str, Any]] = []
    if subject:
        try:
            from corpus_context import prefetch_for_subject

            pack = prefetch_for_subject(conn, subject_label=subject, top_k=memory_top_k)
            related_memory = [
                {
                    "chunk_id": h.get("chunk_id"),
                    "score": h.get("score"),
                    "path": (h.get("path") or "")[:120],
                    "text": (h.get("text") or "")[:200],
                }
                for h in pack.get("hits") or []
            ]
            if related_memory:
                why.append(f"retrieval prefetch for subject {subject!r}")
        except Exception:
            pass

    focus = None
    try:
        from screen_context_store import current_record

        focus = current_record(data_dir)
        if focus:
            why.append("current focus window attached")
    except Exception:
        pass

    bundle_md = _context_bundle_md(
        subject=subject,
        spine_md=spine_md,
        why=why,
        focus=focus,
        evidence=recent_evidence,
        candidates=high_conf,
    )

    reader_id = "mcp" if for_mcp else "local_ui"
    if for_mcp:
        recent_evidence = recent_evidence[:3]

    base = {
        "subject": subject,
        "why_this_context": why,
        "context_md": bundle_md,
        "graph": graph_ctx.get("graph") or {},
        "spine_md": spine_md,
        "open_candidates": candidates,
        "high_confidence_candidates": high_conf,
        "recent_evidence": recent_evidence,
        "related_memory": related_memory,
        "focus": focus,
        "coverage": {
            "blind_collectors": coverage.get("blind_collectors") or [],
            "multi_witness_groups": coverage.get("multi_witness_groups"),
            "raw_event_count": coverage.get("raw_event_count"),
        },
        "freshness": {"generated_at": ts, "evidence_window_hours": 1},
        "for_mcp": for_mcp,
    }
    try:
        from context_platform import enrich_context_bundle

        return enrich_context_bundle(conn, data_dir, base, reader_id=reader_id)
    except Exception:
        log.debug("context_platform enrich skipped", exc_info=True)
        return base


def _context_bundle_md(
    *,
    subject: str,
    spine_md: str,
    why: List[str],
    focus: Optional[Dict[str, Any]],
    evidence: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
) -> str:
    lines = ["## Context bundle"]
    if subject:
        lines.append(f"Subject: **{subject}**")
    if why:
        lines.append("Why: " + "; ".join(why[:6]) + ".")
    if focus:
        lines.append(
            f"Focus: {focus.get('app_name') or focus.get('app')} — "
            f"{(focus.get('window_title') or '')[:80]}"
        )
    if spine_md:
        lines.append("")
        lines.append(spine_md[:1200])
    if evidence:
        lines.append("")
        lines.append("### Recent evidence")
        for e in evidence[:5]:
            wc = e.get("witness_count") or 1
            lines.append(
                f"- {e.get('app')}: {(e.get('scene') or '')[:100]} "
                f"(conf={e.get('confidence')}, witnesses={wc})"
            )
    if candidates:
        lines.append("")
        lines.append("### Pending review")
        for c in candidates[:4]:
            lines.append(f"- {c.get('title')}")
    return "\n".join(lines)
