"""Index Accessibility text samples from screen_context stream into searchable chunks."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ingest import DEFAULT_MODEL, _embed, _get_model
from store import meta_get, meta_set, source_id_for, upsert_source

log = logging.getLogger(__name__)

_META_OFFSET = "ambient_ax_index_byte_offset"
_META_DEDUPE = "ambient_ax_index_dedupe_json"
_DEDUPE_WINDOW_SEC = 300.0


def _slug(s: str, max_len: int = 32) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (t[:max_len] or "app")


def _fingerprint(app: str, title: str, ax: str) -> str:
    norm_ax = re.sub(r"\s+", " ", (ax or "")[:400]).strip().casefold()
    raw = f"{app}\x1f{title}\x1f{norm_ax}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _load_dedupe(meta_json: Optional[str]) -> Dict[str, float]:
    if not meta_json:
        return {}
    try:
        raw = json.loads(meta_json)
        return {str(k): float(v) for k, v in raw.items()}
    except Exception:
        return {}


def _save_dedupe(conn, dedupe: Dict[str, float]) -> None:
    now = time.time()
    pruned = {k: v for k, v in dedupe.items() if now - v < _DEDUPE_WINDOW_SEC * 4}
    meta_set(conn, _META_DEDUPE, json.dumps(pruned))


def index_ax_from_stream(
    *,
    data_dir: Path,
    conn,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Tail-read stream.jsonl from last byte offset; embed new AX samples."""
    base = Path(data_dir).expanduser().resolve()
    path = base / "ambient" / "stream.jsonl"
    if not path.is_file():
        path = base / "screen_context" / "stream.jsonl"
    if not path.is_file():
        return {"indexed": 0, "skipped_no_file": True}

    offset = 0
    raw_off = meta_get(conn, _META_OFFSET)
    if raw_off:
        try:
            offset = int(raw_off)
        except ValueError:
            offset = 0

    size = path.stat().st_size
    if offset > size:
        offset = 0

    with path.open("rb") as f:
        f.seek(offset)
        new_bytes = f.read()
        new_offset = f.tell()

    if not new_bytes:
        return {"indexed": 0, "scanned_bytes": 0, "offset": offset}

    dedupe = _load_dedupe(meta_get(conn, _META_DEDUPE))
    now = time.time()
    indexed = 0
    skipped_dedupe = 0
    model = None

    for line in new_bytes.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ax = str(rec.get("ax_text_sample") or "").strip()
        if not ax:
            continue
        app = str(rec.get("app_name") or "unknown")
        title = str(rec.get("window_title") or "")
        kind = str(rec.get("kind") or "")
        ts = float(rec.get("ts") or now)
        fp = _fingerprint(app, title, ax)
        last = dedupe.get(fp)
        if last is not None and now - last < _DEDUPE_WINDOW_SEC:
            skipped_dedupe += 1
            continue
        dedupe[fp] = ts

        if dry_run:
            indexed += 1
            continue

        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        url = str(rec.get("url") or "")
        host = str(rec.get("url_or_host") or _slug(title) or _slug(app))
        if kind == "window_snapshot":
            rel_path = f"ambient/screen/{day}/{_slug(app)}_{int(ts)}.md"
            header = f"# Screen · {title or app}\n\napp: {app} | ts: {ts}\n\n---\n\n"
        elif url or "chrome" in app.lower() or "safari" in app.lower():
            rel_path = f"ambient/browser/{day}/{_slug(host)}_{int(ts)}.md"
            header = f"# {title or host or app}\n\nurl: {url}\napp: {app} | ts: {ts}\n\n---\n\n"
        else:
            rel_path = f"ambient/ax/{day}/{int(ts)}_{_slug(app)}.md"
            header = f"# Focus: {title or app}\n\napp: {app} | ts: {ts}\n\n---\n\n"
        body = header + ax + "\n"
        sid = source_id_for(rel_path)
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        nodes = rec.get("ax_nodes") or []
        top_roles: List[str] = []
        if isinstance(nodes, list):
            for n in nodes[:8]:
                if isinstance(n, dict) and n.get("role"):
                    top_roles.append(str(n["role"]))
        ax_meta = {
            "captured_at": ts,
            "app": app,
            "title": title,
            "ax_node_count": len(nodes) if isinstance(nodes, list) else 0,
            "ax_top_roles": top_roles,
        }
        if model is None:
            model = _get_model(os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL))
        vecs = _embed(model, [body], on_progress=lambda *_: None)
        upsert_source(
            conn,
            path=rel_path,
            kind="ambient-ax",
            sha256=sha,
            mtime=ts,
            bytes_=len(body.encode("utf-8")),
            parser="ambient_ax",
            source_meta={
                "retention_days": 14,
                "focus_app": app,
                "focus_title": title,
                "url": url,
                "host": host,
                "ax_node_count": ax_meta["ax_node_count"],
            },
            chunks=[(body, "ambient", ax_meta)],
            embeddings=vecs,
        )
        indexed += 1

    if not dry_run:
        meta_set(conn, _META_OFFSET, str(new_offset))
        _save_dedupe(conn, dedupe)

    return {
        "indexed": indexed,
        "skipped_dedupe": skipped_dedupe,
        "scanned_bytes": len(new_bytes),
        "offset_before": offset,
        "offset_after": new_offset if not dry_run else offset,
        "dry_run": dry_run,
    }
