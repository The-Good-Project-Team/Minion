"""Dwell-time and attention summaries from vault-local ambient_events."""
from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from store import ambient_events_since

# Idle/system "apps" that aren't real attention (lock screen etc.).
IDLE_APPS = frozenset({"", "loginwindow", "ScreenSaverEngine", "WindowServer", "UserNotificationCenter"})
# Gaps longer than this between focus events are away/idle time, not attention.
_GAP_CAP_SEC = 300.0


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
            # Attribute the gap to the PREVIOUS focus, but only if it's real attention
            # (capped) and not an idle/system app — otherwise the lock screen dominates.
            if last_ts is not None and last_app and _base_app(last_app) not in IDLE_APPS:
                gap = ts - last_ts
                if 0.0 < gap <= _GAP_CAP_SEC:
                    dwell[last_app] += gap
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


def _base_app(label: str) -> str:
    """The app name from a 'App — title' dwell key."""
    return label.split(" — ", 1)[0].strip()


def recent_work_digest(
    conn,
    *,
    since_ts: float,
    gap_cap_sec: float = _GAP_CAP_SEC,
    limit: int = 20000,
    signals_per_app: int = 4,
    top_apps: int = 8,
) -> Dict[str, Any]:
    """Deliver 'what have I been working on' directly: active attention per APP (idle
    excluded, idle gaps capped) + the actual on-screen work signals per app, pulled
    from the ingested ambient-ax chunks. One call, no client-side computation."""
    events = sorted(
        ambient_events_since(conn, since_ts=since_ts, limit=limit),
        key=lambda e: float(e.get("captured_at") or 0),
    )
    active: Dict[str, float] = defaultdict(float)
    span_lo = span_hi = 0.0
    rows: List = []
    for e in events:
        et = str(e.get("event_type") or "")
        if et not in ("window_focus", "ax_content_changed", "window_snapshot", "app_launched"):
            continue
        ts = float(e.get("captured_at") or 0)
        app = str((e.get("payload") or {}).get("app_name") or "").strip()
        rows.append((ts, app))
    for i in range(len(rows) - 1):
        ts, app = rows[i]
        if app and app not in IDLE_APPS:
            gap = rows[i + 1][0] - ts
            if 0.0 < gap <= gap_cap_sec:
                active[app] += gap
            span_lo = span_lo or ts
            span_hi = ts

    total = sum(active.values()) or 1.0
    apps = [
        {"app": a, "minutes": round(s / 60.0, 1), "pct": round(100.0 * s / total, 1)}
        for a, s in sorted(active.items(), key=lambda x: -x[1])[:top_apps]
    ]

    # On-screen work signals from the ingested ambient-ax chunks (the searchable text).
    signals: Dict[str, List[str]] = {}
    seen: Dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute(
        "SELECT c.text FROM chunks c JOIN sources s ON s.source_id = c.source_id "
        "WHERE s.kind = 'ambient-ax'"
    ).fetchall():
        text = str(r[0] or "")
        m = re.search(r"app:\s*([^|]+)", text)
        app = (m.group(1).strip() if m else "")
        mt = re.search(r"ts:\s*([0-9.]+)", text)
        if mt and float(mt.group(1)) < since_ts:
            continue
        if not app or app in IDLE_APPS:
            continue
        body = text.split("---", 1)[1] if "---" in text else text
        line = " ".join(body.split())[:80]
        if len(line) > 3:
            seen[app][line] += 1
    top_app_names = {a["app"] for a in apps}
    for app in top_app_names:
        sigs = [ln for ln, _ in seen.get(app, Counter()).most_common(signals_per_app)]
        if sigs:
            signals[app] = sigs

    return {
        "window_start": span_lo or since_ts,
        "window_end": span_hi or time.time(),
        "active_minutes": round(total / 60.0, 1),
        "apps_by_attention": apps,
        "work_signals": signals,
        "summary_line": "; ".join(f"{a['app']} {a['pct']}%" for a in apps[:4]) or "no activity in window",
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
