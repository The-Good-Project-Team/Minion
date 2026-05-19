"""Dwell-time and attention summaries from vault-local ambient_events."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from store import ambient_events_since


def _host_from_payload(payload: Dict[str, Any]) -> str:
    url = str(payload.get("url") or "").strip()
    if url.startswith("http"):
        try:
            return urlparse(url).netloc or url[:80]
        except Exception:
            pass
    raw = str(
        payload.get("url_or_host")
        or payload.get("host")
        or payload.get("window_title")
        or ""
    ).strip()
    if not raw:
        return ""
    if "://" in raw:
        try:
            return urlparse(raw).netloc or raw[:80]
        except Exception:
            return raw[:80]
    if raw.startswith("http"):
        try:
            return urlparse(raw).netloc or raw[:80]
        except Exception:
            pass
    return raw[:80]


def rollup_attention(
    conn,
    *,
    since_ts: float,
    limit: int = 500,
) -> Dict[str, Any]:
    events = ambient_events_since(conn, since_ts=since_ts, limit=limit)
    events = sorted(events, key=lambda e: float(e.get("captured_at") or 0))

    dwell: Dict[str, float] = defaultdict(float)
    hosts: Counter[str] = Counter()
    fs_hotspots: Counter[str] = Counter()
    listening_sec = 0.0
    last_ts: Optional[float] = None
    last_app = ""

    for e in events:
        ts = float(e.get("captured_at") or 0)
        et = str(e.get("event_type") or "")
        payload = e.get("payload") or {}

        if et in ("window_focus", "ax_content_changed"):
            app = str(payload.get("app_name") or "?")
            title = str(payload.get("window_title") or "")
            key = f"{app} — {title}" if title else app
            if last_ts is not None and last_app:
                dwell[last_app] += max(0.0, ts - last_ts)
            last_ts = ts
            last_app = key

        elif et == "browser_visit":
            h = _host_from_payload(payload)
            if h:
                hosts[h] += 1

        elif et == "fs_event":
            path = str(payload.get("path") or "")
            if path:
                fs_hotspots[_basename(path)] += 1

        elif et == "listening_chunk":
            dur = float(payload.get("duration_sec") or 30.0)
            listening_sec += dur

    top_apps = [
        {"label": k, "dwell_sec": round(v, 1)}
        for k, v in sorted(dwell.items(), key=lambda x: -x[1])[:12]
    ]
    top_hosts = [{"host": h, "visits": c} for h, c in hosts.most_common(10)]
    top_fs = [{"basename": b, "events": c} for b, c in fs_hotspots.most_common(8)]

    return {
        "event_count": len(events),
        "top_apps": top_apps,
        "top_hosts": top_hosts,
        "fs_hotspots": top_fs,
        "listening_minutes": round(listening_sec / 60.0, 1),
        "summary_line": _summary_line(top_apps, top_hosts, listening_sec),
    }


def _basename(path: str) -> str:
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return parts[-1] if parts else path


def _summary_line(
    top_apps: List[Dict[str, Any]],
    top_hosts: List[Dict[str, Any]],
    listening_sec: float,
) -> str:
    parts: List[str] = []
    if top_apps:
        a = top_apps[0]
        parts.append(f"Most time: {a.get('label', '?')}")
    if top_hosts:
        parts.append(f"Top site: {top_hosts[0].get('host', '?')}")
    if listening_sec > 60:
        parts.append(f"Listening {int(listening_sec // 60)}m")
    return "; ".join(parts) if parts else "No attention signals in window."


def attention_excerpt_for_mcp(rollup: Dict[str, Any], *, max_apps: int = 4) -> str:
    """Titles-only excerpt safe for working_context."""
    apps = rollup.get("top_apps") or []
    labels = [str(a.get("label") or "")[:60] for a in apps[:max_apps] if a.get("label")]
    line = rollup.get("summary_line") or ""
    if labels:
        return f"{line} ({', '.join(labels)})" if line else ", ".join(labels)
    return line
