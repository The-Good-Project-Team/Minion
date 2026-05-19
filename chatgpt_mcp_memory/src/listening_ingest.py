"""Transcribe completed listening-session WAV segments into ambient events + chunks."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict

from ambient_pipeline import ingest_ambient_jsonl
from store import ambient_event_insert_ignore, meta_get, meta_set, source_id_for, upsert_source

log = logging.getLogger(__name__)

_WAKE_RE = re.compile(r"\bminion\b", re.IGNORECASE)
_WAKE_NOTIFY = "ambient/listening_wake_notify.json"


def _wake_word_in_text(text: str) -> bool:
    return bool(_WAKE_RE.search(text))


def _emit_listening_wake(*, data_dir: Path, excerpt: str, ts: float, session_id: str, wav_name: str) -> None:
    base = Path(data_dir).expanduser().resolve()
    rec = {
        "ts": ts,
        "kind": "listening_wake",
        "session_id": session_id,
        "transcript_excerpt": excerpt[:400],
        "wav": wav_name,
        "dedupe_key": f"listen_wake:{session_id}:{wav_name}",
    }
    stream = base / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    with stream.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    notify = base / _WAKE_NOTIFY
    notify.write_text(
        json.dumps({"excerpt": excerpt[:200], "ts": ts}, ensure_ascii=False),
        encoding="utf-8",
    )


def ingest_listening_segments(*, data_dir: Path, conn: Any) -> Dict[str, Any]:
    """Scan `<data_dir>/ambient/listening/*/*.wav` and emit transcript chunks."""
    base = Path(data_dir).expanduser().resolve() / "ambient" / "listening"
    if not base.is_dir():
        return {"transcribed": 0, "skipped": True}

    transcribed = 0
    for session_dir in sorted(base.iterdir()):
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        for wav in sorted(session_dir.glob("*.wav")):
            key = f"{session_id}/{wav.name}"
            done_key = f"listening_done:{key}"
            if meta_get(conn, done_key):
                continue
            try:
                from parsers.audio import parse

                result = parse(wav)
            except Exception as e:
                log.warning("listening transcribe failed %s: %s", wav, e)
                continue
            text = " ".join(c.text for c in result.chunks if c.text).strip()
            if not text:
                meta_set(conn, done_key, "empty")
                continue
            ts = wav.stat().st_mtime
            rel = f"ambient/listening/{session_id}/{wav.stem}.md"
            body = f"# Listening session {session_id}\n\n{text}\n"
            sid = source_id_for(rel)
            vecs = None
            try:
                from ingest import DEFAULT_MODEL, _embed, _get_model
                import os

                model = _get_model(os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL))
                vecs = _embed(model, [body], on_progress=lambda *_: None)
            except Exception:
                log.warning("listening embed skipped for %s", wav)
            if vecs is None:
                meta_set(conn, done_key, "no_embed")
                continue
            upsert_source(
                conn,
                path=rel,
                kind="ambient-audio",
                sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                mtime=ts,
                bytes_=len(body.encode("utf-8")),
                parser="listening_whisper",
                source_meta={"retention_days": 14, "session_id": session_id},
                chunks=[(body, "ambient", {"session_id": session_id})],
                embeddings=vecs,
            )
            ambient_event_insert_ignore(
                conn,
                event_type="listening_chunk",
                captured_at=ts,
                dedupe_key=f"listen:{session_id}:{wav.name}",
                payload={
                    "kind": "listening_chunk",
                    "session_id": session_id,
                    "wav": str(wav.name),
                    "transcript_excerpt": text[:400],
                    "duration_sec": 30.0,
                },
                sensitivity="vault_local",
                storage_tier="hot",
            )
            meta_set(conn, done_key, "1")
            transcribed += 1
            if _wake_word_in_text(text):
                _emit_listening_wake(
                    data_dir=Path(data_dir),
                    excerpt=text,
                    ts=ts,
                    session_id=session_id,
                    wav_name=wav.name,
                )

    amb = ingest_ambient_jsonl(data_dir=data_dir, conn=conn, max_lines=500)
    return {"transcribed": transcribed, "ambient": amb}
