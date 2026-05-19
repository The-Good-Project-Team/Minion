"""Roll up recent ambient events into searchable ``ambient-summary`` chunks."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import hashlib

from ingest import DEFAULT_MODEL, _embed, _get_model
from store import meta_get, meta_set, upsert_source

log = logging.getLogger(__name__)

_META_LAST = "ambient_consolidation_last_run"
_SKIP_TYPES = frozenset({"fs_event", "process_snapshot", "app_launched"})


def _hour_key(ts: float, app: str) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{app}\x1f{dt.strftime('%Y-%m-%dT%H')}"


def run_ambient_consolidation(conn, data_dir: Path, *, force: bool = False) -> Dict[str, Any]:
    """Build hourly per-app summaries for the last 24h (idempotent per group key)."""
    now = time.time()
    last = 0.0
    raw = meta_get(conn, _META_LAST)
    if raw:
        try:
            last = float(raw)
        except ValueError:
            pass
    if not force and now - last < 6 * 3600:
        return {"skipped": True, "reason": "interval"}

    cutoff = now - 86400.0
    rows = conn.execute(
        """
        SELECT event_type, captured_at, payload_json
        FROM ambient_events
        WHERE captured_at >= ?
        ORDER BY captured_at ASC
        """,
        (cutoff,),
    ).fetchall()

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        et = str(r["event_type"] or "")
        if et in _SKIP_TYPES:
            continue
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        ts = float(r["captured_at"] or 0)
        app = str(payload.get("app_name") or "?")
        groups[_hour_key(ts, app)].append(
            {"event_type": et, "ts": ts, "payload": payload}
        )

    model = _get_model(DEFAULT_MODEL)
    written = 0
    for gkey, events in groups.items():
        if len(events) < 2:
            continue
        app, hour = gkey.split("\x1f", 1)
        titles = Counter(
            str(e["payload"].get("window_title") or "")[:120]
            for e in events
            if e["payload"].get("window_title")
        )
        urls = [
            str(e["payload"].get("url") or e["payload"].get("url_or_host") or "")
            for e in events
            if e["payload"].get("url") or e["payload"].get("url_or_host")
        ]
        url_sample = urls[-1][:200] if urls else ""
        kinds = Counter(str(e["event_type"]) for e in events)
        title_lines = "\n".join(f"  - {t} ({c})" for t, c in titles.most_common(5))
        kind_line = ", ".join(f"{k}:{v}" for k, v in kinds.most_common(6))
        body = (
            f"Ambient summary for {app} during {hour} UTC.\n"
            f"Events: {len(events)} ({kind_line}).\n"
            f"Top window titles:\n{title_lines or '  (none)'}\n"
        )
        if url_sample:
            body += f"Sample URL: {url_sample}\n"

        rel = f"ambient/summary/{hour.replace(':', '-')}_{_slug(app)}.md"
        meta = {
            "group_key": gkey,
            "app_name": app,
            "hour": hour,
            "event_count": len(events),
        }
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        vecs = _embed(model, [body], on_progress=lambda *_: None)
        upsert_source(
            conn,
            path=rel,
            kind="ambient-summary",
            sha256=sha,
            mtime=now,
            bytes_=len(body.encode("utf-8")),
            parser="ambient_consolidation",
            source_meta=meta,
            chunks=[(body, "summary", meta)],
            embeddings=vecs,
        )
        written += 1

    meta_set(conn, _META_LAST, str(now))
    return {"skipped": False, "groups": len(groups), "summaries_written": written}


def _slug(s: str, max_len: int = 24) -> str:
    import re

    t = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (t[:max_len] or "app")
