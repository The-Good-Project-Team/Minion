"""Normalize ambient JSONL streams into typed `ambient_events` rows."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from settings import ambient_capture_denied, load_settings
from store import ambient_event_insert_ignore

log = logging.getLogger(__name__)

Handler = Callable[[Any, Dict[str, Any], float], Optional[Tuple[str, str]]]


def _stream_paths(data_dir: Path) -> List[Path]:
    base = Path(data_dir).expanduser().resolve()
    paths = [base / "ambient" / "stream.jsonl", base / "screen_context" / "stream.jsonl"]
    return [p for p in paths if p.is_file()]


def _secret_path(path: str) -> bool:
    low = path.lower()
    if any(
        x in low
        for x in (".env", "id_rsa", ".pem", "keychain", "credentials", "secrets")
    ):
        return True
    return bool(re.search(r"\.(pem|p12|pfx|key)$", low))


def _ingest_window_focus(_conn, rec: Dict[str, Any], ts: float) -> Tuple[str, str]:
    app = str(rec.get("app_name") or "")
    title = str(rec.get("window_title") or "")
    dedupe_key = f"wf:{ts:.6f}:{app}\x1f{title}"
    return "window_focus", dedupe_key


def _ingest_ax_changed(_conn, rec: Dict[str, Any], ts: float) -> Tuple[str, str]:
    app = str(rec.get("app_name") or "")
    title = str(rec.get("window_title") or "")
    axh = str(rec.get("ax_hash") or "")
    dedupe_key = f"ax:{app}\x1f{title}\x1f{axh}"
    return "ax_content_changed", dedupe_key


def _ingest_generic(_conn, rec: Dict[str, Any], ts: float) -> Tuple[str, str]:
    kind = str(rec.get("kind") or "ambient")
    dk = str(rec.get("dedupe_key") or "")
    if not dk:
        dk = f"{kind}:{ts:.3f}"
    return kind, dk


def _ingest_browser(_conn, rec: Dict[str, Any], ts: float) -> Tuple[str, str]:
    host = str(rec.get("url_or_host") or rec.get("host") or "?")
    minute = int(ts // 60)
    browser = str(rec.get("browser") or rec.get("app_name") or "")
    dedupe_key = f"browser:{browser}:{host}:{minute}"
    return "browser_visit", dedupe_key


def _ingest_listening(_conn, rec: Dict[str, Any], ts: float) -> Tuple[str, str]:
    kind = str(rec.get("kind") or "listening_chunk")
    sid = str(rec.get("session_id") or "")
    dedupe_key = str(rec.get("dedupe_key") or f"{kind}:{sid}:{ts:.3f}")
    return kind, dedupe_key


def _ingest_screenshot(_conn, rec: Dict[str, Any], ts: float) -> Tuple[str, str]:
    rel = str(rec.get("screenshot_inbox_rel") or "").strip()
    dedupe = f"shot:{rel}" if rel else f"shot:{ts:.3f}"
    return "screenshot_fallback", dedupe


def _ingest_window_snapshot(_conn, rec: Dict[str, Any], ts: float) -> Tuple[str, str]:
    wid = str(rec.get("window_id") or "")
    axh = str(rec.get("ax_hash") or "")
    dk = str(rec.get("dedupe_key") or "")
    if not dk:
        dk = f"ws:{wid}:{axh}" if wid or axh else f"window_snapshot:{ts:.3f}"
    return "window_snapshot", dk


INGEST_HANDLERS: Dict[str, Handler] = {
    "window_focus": _ingest_window_focus,
    "ax_content_changed": _ingest_ax_changed,
    "process_snapshot": _ingest_generic,
    "app_launched": _ingest_generic,
    "browser_visit": _ingest_browser,
    "listening_start": _ingest_listening,
    "listening_chunk": _ingest_listening,
    "listening_end": _ingest_listening,
    "listening_wake": _ingest_listening,
    "full_listening_start": _ingest_listening,
    "full_listening_end": _ingest_listening,
    "screenshot_fallback": _ingest_screenshot,
    "window_snapshot": _ingest_window_snapshot,
    "dom_snapshot": _ingest_generic,
    "mouse_event": _ingest_generic,
    "keyboard_event": _ingest_generic,
    "clipboard_event": _ingest_generic,
    "rolling_video_clip": _ingest_generic,
    "marlin_event": _ingest_generic,
    "omniparser_parse": _ingest_generic,
    "general_vlm": _ingest_generic,
    "vlm_reasoning": _ingest_generic,
    "vlm_parse": _ingest_generic,
}


def ingest_ambient_jsonl(
    *,
    data_dir: Path,
    conn: Any,
    max_lines: int = 1200,
) -> Dict[str, Any]:
    """Tail-read ambient JSONL files, insert deduped events."""
    paths = _stream_paths(data_dir)
    if not paths:
        return {"ingested": 0, "skipped_no_file": True, "by_kind": {}}

    all_lines: List[str] = []
    for path in paths:
        try:
            all_lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError as e:
            log.warning("ambient ingest could not read %s: %s", path, e)

    tail = all_lines[-max(1, min(max_lines, 50_000)) :]
    settings = load_settings(data_dir)
    inserted = 0
    by_kind: Dict[str, int] = {}
    for ln in tail:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        kind = str(rec.get("kind") or "window_focus")
        if kind == "fs_event":
            continue
        app = str(rec.get("app_name") or "")
        title = str(rec.get("window_title") or "")
        if ambient_capture_denied(settings, app, title):
            continue
        handler = INGEST_HANDLERS.get(kind)
        if not handler:
            continue
        try:
            ts = float(rec.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        try:
            resolved = handler(conn, rec, ts)
        except Exception:
            log.exception("ambient handler failed for kind=%s", kind)
            continue
        if resolved is None:
            continue
        event_type, dedupe_key = resolved
        payload = {k: v for k, v in rec.items()}
        try:
            if ambient_event_insert_ignore(
                conn,
                event_type=event_type,
                captured_at=ts,
                dedupe_key=dedupe_key,
                payload=payload,
                sensitivity="vault_local",
                storage_tier="hot",
            ):
                inserted += 1
                by_kind[event_type] = by_kind.get(event_type, 0) + 1
        except Exception:
            log.exception("ambient_event_insert_ignore failed")
    return {"ingested": inserted, "scanned_lines": len(tail), "by_kind": by_kind}


def ingest_screen_context_jsonl(
    *,
    data_dir: Path,
    conn: Any,
    max_lines: int = 1200,
) -> Dict[str, Any]:
    """Backward-compatible alias."""
    return ingest_ambient_jsonl(data_dir=data_dir, conn=conn, max_lines=max_lines)
