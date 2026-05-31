"""Full-text screen reading on ANY app.

OCRs captured screenshots (RapidOCR) and indexes the recognized text as searchable,
app/window/time-tagged chunks (kind="screen-ocr"). App-agnostic: whatever is visible on
screen — web page body, terminal, native app, PDF viewer — becomes searchable, including
content the accessibility tree and DOM miss (e.g. canvas/Canva, video, images of text).

Reuses minion's embedding (ingest._get_model/_embed) and OCR (parsers.image._ocr_rapidocr),
and is idempotent per screenshot path. Wired into the ambient loop (a few per tick) and
runnable in bulk.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ingest import DEFAULT_MODEL, _embed, _get_model
from store import get_source_by_path, upsert_source

log = logging.getLogger(__name__)

OCR_KIND = "screen-ocr"
_MIN_CHARS = 12
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _screenshot_files(data_dir: Path) -> List[Path]:
    dirs = [data_dir / "inbox" / "screen-memory", data_dir / "ambient" / "screenshots"]
    out: List[Path] = []
    for d in dirs:
        if d.is_dir():
            out.extend(p for p in d.iterdir() if p.suffix.lower() in _IMG_EXTS)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def _app_window_for(conn, basename: str) -> Tuple[str, str, float]:
    """Find the ambient event whose screenshot_inbox_rel matches this file → app/title/ts."""
    row = conn.execute(
        "SELECT payload_json, captured_at FROM ambient_events "
        "WHERE payload_json LIKE ? ORDER BY captured_at DESC LIMIT 1",
        (f"%{basename}%",),
    ).fetchone()
    if row:
        try:
            p = json.loads(row[0] or "{}")
        except Exception:
            p = {}
        return (
            str(p.get("app_name") or p.get("app") or "").strip(),
            str(p.get("window_title") or p.get("title") or "").strip(),
            float(row[1] or 0),
        )
    # Fall back to a timestamp embedded in the filename (e.g. wsnap_1779225819_5339.png).
    m = re.search(r"(\d{9,})", basename)
    return ("", "", float(m.group(1)) if m else 0.0)


def index_screenshots(
    conn,
    data_dir: Path,
    *,
    limit: int = 200,
    model: Optional[Any] = None,
) -> Dict[str, Any]:
    """OCR + index screenshots not already indexed. Returns {indexed, skipped, empty}."""
    from parsers.image import _ocr_rapidocr

    indexed = skipped = empty = 0
    for shot in _screenshot_files(data_dir)[:limit]:
        rel_path = f"screen-ocr/{shot.name}"
        if get_source_by_path(conn, rel_path) is not None:
            skipped += 1
            continue
        try:
            text, err = _ocr_rapidocr(shot)
        except Exception as e:
            err, text = str(e), ""
        text = (text or "").strip()
        if err or len(text) < _MIN_CHARS:
            empty += 1
            continue
        app, title, ts = _app_window_for(conn, shot.name)
        ts = ts or shot.stat().st_mtime
        header = f"# Screen OCR · {title or app or 'screen'}\n\napp: {app} | ts: {ts}\n\n---\n\n"
        body = header + text + "\n"
        if model is None:
            model = _get_model(os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL))
        vecs = _embed(model, [body], on_progress=lambda *_: None)
        upsert_source(
            conn,
            path=rel_path,
            kind=OCR_KIND,
            sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            mtime=ts,
            bytes_=len(body.encode("utf-8")),
            parser="screen_ocr",
            source_meta={"app": app, "title": title, "screenshot": shot.name},
            chunks=[(body, "screen", {"app": app, "title": title, "captured_at": ts})],
            embeddings=vecs,
        )
        indexed += 1
    if indexed:
        conn.commit()
    return {"indexed": indexed, "skipped": skipped, "empty": empty}
