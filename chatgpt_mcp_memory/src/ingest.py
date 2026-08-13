"""
Ingestion pipeline: file path -> parser -> fastembed -> SQLite store.

This is the single choke-point every writer uses (watcher, `minion add`,
rebuild scripts). Keep it tiny and side-effect-free apart from DB writes
and model load.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from typing import Any, Callable, Dict

import numpy as np

from fastembed_cache import fastembed_cache_dir
from parsers import (
    ALL_KINDS,
    ParseResult,
    UnsupportedFile,
    disabled_kinds,
    is_disabled_kind,
    kind_for,
    parse_file,
)
from store import profile_get_active, sha256_of_file, upsert_source
import telemetry


def _chatgpt_export_manifest_paths(root: Path) -> List[Path]:
    """Return the set of JSON files that define this ChatGPT export.

    Covers both the native OpenAI layout (`conversations*.json` at root)
    and the third-party per-conversation layout (`json/YYYY-MM-DD_*.json`).
    Empty list means this directory is not a recognized export.
    """
    native = sorted(root.glob("conversations*.json"))
    if native:
        return native
    per_conv = sorted(root.glob("json/[12][0-9][0-9][0-9]-*.json"))
    return per_conv


def _looks_like_chatgpt_export(path: Path) -> bool:
    # Claude exports also ship a `conversations.json`; disambiguate by content
    # so a Claude export is never routed to the ChatGPT parser.
    return (
        path.is_dir()
        and bool(_chatgpt_export_manifest_paths(path))
        and not _looks_like_claude_export(path)
    )


def _claude_export_manifest_paths(root: Path) -> List[Path]:
    """JSON files that define a Claude.ai export.

    Claude ships a single `conversations.json` (some chunked exports use
    `conversations-*.json`). Empty list means this directory is not one.
    """
    native = sorted(root.glob("conversations.json"))
    if native:
        return native
    return sorted(root.glob("conversations-*.json"))


def _peek_is_claude_export(manifest: Path) -> bool:
    """Cheap content sniff: a Claude conversation has `chat_messages`; a
    ChatGPT one has a `mapping` tree. Read a bounded prefix to decide without
    parsing a multi-MB manifest."""
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            head = fh.read(1_000_000)
    except OSError:
        return False
    return '"chat_messages"' in head


def _looks_like_claude_export(path: Path) -> bool:
    if not path.is_dir():
        return False
    manifests = _claude_export_manifest_paths(path)
    if not manifests:
        return False
    # Sniff the first manifest for Claude's "chat_messages" key
    try:
        with open(manifests[0], "r", encoding="utf-8") as fh:
            head = fh.read(2000)
        return '"chat_messages"' in head
    except (OSError, UnicodeDecodeError):
        return False


def _looks_like_gemini_export(path: Path) -> bool:
    """Detect Gemini exports by looking for conversation JSON files."""
    if not path.is_dir():
        return False
    # Look for Gemini-specific patterns
    if list(path.glob("conversations.json")) or list(path.glob("conversation*.json")):
        # Sniff to confirm it's not ChatGPT, Claude, or Copilot
        try:
            for manifest in sorted(path.glob("*.json"))[:3]:
                with open(manifest, "r", encoding="utf-8") as fh:
                    head = fh.read(2000)
                # Gemini exports typically have "messages", "history", or "turns" keys
                # but not "mapping" (ChatGPT) or "chat_messages" (Claude)
                if '"mapping"' in head or '"chat_messages"' in head:
                    return False
                if '"messages"' in head or '"history"' in head or '"turns"' in head:
                    return True
        except (OSError, UnicodeDecodeError):
            pass
    return False


def _looks_like_copilot_export(path: Path) -> bool:
    """Detect Copilot exports by looking for conversation JSON files."""
    if not path.is_dir():
        return False
    # Look for Copilot-specific patterns
    if list(path.glob("conversations.json")) or list(path.glob("copilot*.json")) or list(path.glob("conversation*.json")):
        # Sniff to confirm it's not ChatGPT, Claude, or Gemini
        try:
            for manifest in sorted(path.glob("*.json"))[:3]:
                with open(manifest, "r", encoding="utf-8") as fh:
                    head = fh.read(2000)
                # Copilot exports typically have "messages", "content", or "body" keys
                # but not "mapping" (ChatGPT) or "chat_messages" (Claude)
                if '"mapping"' in head or '"chat_messages"' in head:
                    return False
                # Copilot often has different structure - look for common patterns
                if '"content"' in head or '"body"' in head or '"copilot"' in head.lower():
                    return True
        except (OSError, UnicodeDecodeError):
            pass
    return False


def _copilot_export_manifest_paths(path: Path) -> List[Path]:
    """Find all JSON manifest files in a Copilot export."""
    paths = sorted(path.glob("conversations.json")) or sorted(path.glob("copilot*.json")) or sorted(path.glob("conversation*.json")) or sorted(path.glob("*.json"))
    return [p for p in paths if p.is_file()]


def _copilot_export_digest(path: Path, manifests: List[Path]) -> str:
    """Compute a digest over the manifest files for change detection."""
    import hashlib
    h = hashlib.sha256()
    for p in sorted(manifests):
        stat = p.stat()
        h.update(str(p.relative_to(path)).encode())
        h.update(str(stat.st_size).encode())
        h.update(str(stat.st_mtime).encode())
    return h.hexdigest()


def _gemini_export_manifest_paths(path: Path) -> List[Path]:
    """Find all JSON manifest files in a Gemini export."""
    paths = sorted(path.glob("conversations.json")) or sorted(path.glob("conversation*.json")) or sorted(path.glob("*.json"))
    return [p for p in paths if p.is_file()]


def _gemini_export_digest(path: Path, manifests: List[Path]) -> str:
    """Compute a digest over the manifest files for change detection."""
    import hashlib
    h = hashlib.sha256()
    for p in sorted(manifests):
        stat = p.stat()
        h.update(str(p.relative_to(path)).encode())
        h.update(str(stat.st_size).encode())
        h.update(str(stat.st_mtime).encode())
    return h.hexdigest()


def _peek_is_claude_export(manifest: Path) -> bool:
    """Quick sniff to check if a manifest is a Claude export."""
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            head = fh.read(2000)
        return '"chat_messages"' in head
    except (OSError, UnicodeDecodeError):
        return False


def _claude_export_digest(root: Path, manifests: List[Path]) -> str:
    """Deterministic digest over (relpath, size, mtime) for dedup."""
    import hashlib

    h = hashlib.sha256()
    for p in manifests:
        rel = p.relative_to(root).as_posix().encode("utf-8")
        st = p.stat()
        h.update(rel)
        h.update(b"\x00")
        h.update(str(st.st_size).encode("ascii"))
        h.update(b"\x00")
        h.update(f"{st.st_mtime:.6f}".encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _chatgpt_export_digest(root: Path, manifests: List[Path]) -> str:
    """Deterministic digest over (relpath, size, mtime) for dedup.

    Cheap: no file reads, just stat. Invalidates cache when any manifest is
    added, removed, resized, or rewritten.
    """
    import hashlib

    h = hashlib.sha256()
    for p in manifests:
        rel = p.relative_to(root).as_posix().encode("utf-8")
        st = p.stat()
        h.update(rel)
        h.update(b"\x00")
        h.update(str(st.st_size).encode("ascii"))
        h.update(b"\x00")
        h.update(f"{st.st_mtime:.6f}".encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


ProgressFn = Callable[[str, Dict[str, Any]], None]


def _noop(_stage: str, _info: Dict[str, Any]) -> None:
    pass


# Archive formats we unpack in-place. The contents are ingested individually
# by the watcher's next pass; we don't try to parse the archive itself.
_ARCHIVE_EXTS = (".zip",)

# Conservative cap so DALL-E-style 400-char basenames inside ChatGPT exports
# don't blow past macOS's 255-byte filename limit.
_MAX_BASENAME_BYTES = 200


class DeterministicTextEmbedding:
    """Small local embedding model for auditable tests; no network/model download."""

    def __init__(self, *, dim: Optional[int] = None) -> None:
        # Default tracks the live embed dim so test DBs (which bootstrap at
        # store.DEFAULT_EMBED_DIM) and the deterministic embedder never disagree.
        self.dim = int(dim or os.environ.get("MINION_TEST_EMBED_DIM") or 768)

    def embed(self, texts: List[str], batch_size: int = 64):
        for text in texts:
            vec = np.zeros((self.dim,), dtype=np.float32)
            for token in re.findall(r"[a-z0-9]{2,}", (text or "").lower()):
                h = int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:8], 16)
                vec[h % self.dim] += 1.0
            norm = float(np.linalg.norm(vec))
            yield vec / norm if norm > 0 else vec


def deterministic_embeddings_enabled() -> bool:
    return os.environ.get("MINION_DETERMINISTIC_EMBEDDINGS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _unique_dir(parent: Path, name: str) -> Path:
    dest = parent / name
    if not dest.exists():
        return dest
    n = 1
    while True:
        cand = parent / f"{name} ({n})"
        if not cand.exists():
            return cand
        n += 1


def _unpack_zip(archive: Path, *, on_progress: ProgressFn = _noop) -> tuple[Path, int, int]:
    """Unpack `archive` into a sibling directory.

    Skips overlong basenames (media with 400+ char names from ChatGPT exports)
    and any zip entries that would escape the destination root (zip-slip).
    Returns (dest_dir, extracted_count, skipped_count).
    """
    import zipfile

    dest = _unique_dir(archive.parent, archive.stem)
    dest.mkdir(parents=True, exist_ok=False)
    on_progress("unpack_start", {"archive": str(archive), "dest": str(dest)})

    extracted = 0
    skipped = 0
    dest_root = dest.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        infos = zf.infolist()
        total = len(infos)
        for i, info in enumerate(infos, 1):
            if info.is_dir():
                continue
            basename = Path(info.filename).name
            if not basename:
                skipped += 1
                continue
            if len(basename.encode("utf-8", "replace")) > _MAX_BASENAME_BYTES:
                skipped += 1
                continue
            target = (dest / info.filename).resolve()
            # Zip-slip guard: ensure the resolved path stays under dest.
            try:
                target.relative_to(dest_root)
            except ValueError:
                skipped += 1
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    # Stream to avoid loading gigabyte files into RAM.
                    while True:
                        buf = src.read(1 << 20)  # 1 MiB chunks
                        if not buf:
                            break
                        out.write(buf)
                extracted += 1
            except OSError:
                skipped += 1
                continue
            if i % 50 == 0 or i == total:
                on_progress(
                    "unpack_progress",
                    {"done": i, "total": total, "extracted": extracted, "skipped": skipped},
                )
    on_progress("unpack_done", {"extracted": extracted, "skipped": skipped, "dest": str(dest)})
    return dest, extracted, skipped


def _maybe_unpack_archive(path: Path, *, on_progress: ProgressFn = _noop) -> Optional["IngestResult"]:
    """If `path` is an archive, unpack it and remove the original.

    Returns an IngestResult describing the unpack so the caller can record it
    in the feed. Returns None if `path` isn't an archive (normal ingest path).
    """
    suffix = path.suffix.lower()
    if suffix not in _ARCHIVE_EXTS:
        return None

    try:
        dest, extracted, skipped = _unpack_zip(path, on_progress=on_progress)
    except Exception as e:
        return IngestResult(
            str(path), None, "archive", "archive-unpack", 0, True,
            reason=f"unpack failed: {type(e).__name__}: {e}",
        )

    # Remove the archive so the watcher doesn't re-unpack it on every tick.
    try:
        path.unlink()
    except OSError:
        pass

    reason = f"unpacked {extracted} file(s) into {dest.name}/"
    if skipped:
        reason += f" (skipped {skipped} over-long or unsafe entries)"
    return IngestResult(
        str(path), None, "archive", "archive-unpack", 0, True,
        reason=reason,
    )


DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"
# Embedding width of DEFAULT_MODEL. Used only for empty-input zero-shapes; live
# dims always come from the DB via store.get_embed_dim.
DEFAULT_MODEL_DIM = 768

# BGE retrieval is asymmetric: the *query* gets an instruction prefix, passages
# do not. Skipping the query prefix measurably hurts recall, so route every
# query-embed through `apply_query_prefix`. Non-bge models are passed through
# untouched (the deterministic test embedder and MiniLM need no prefix).
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


def uses_query_prefix(model_name: str) -> bool:
    name = (model_name or "").lower()
    # bge-m3 / bge-*-icl are instruction-free; classic bge v1.5 en wants it.
    return "bge" in name and "m3" not in name


def apply_query_prefix(model_name: str, query: str) -> str:
    """Prepend the bge query instruction for bge models; identity otherwise."""
    return f"{BGE_QUERY_INSTRUCTION}{query}" if uses_query_prefix(model_name) else query


_MODEL_LOCK = threading.Lock()
_MODEL = None
_MODEL_NAME: Optional[str] = None


def _get_model(name: str):
    """Cache the fastembed model. Safe to call from multiple threads."""
    if deterministic_embeddings_enabled():
        return DeterministicTextEmbedding()
    global _MODEL, _MODEL_NAME
    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_NAME == name:
            return _MODEL
        from fastembed import TextEmbedding

        _MODEL = TextEmbedding(model_name=name, cache_dir=fastembed_cache_dir())
        _MODEL_NAME = name
        return _MODEL


@dataclass
class IngestResult:
    path: str
    source_id: Optional[str]
    kind: str
    parser: str
    chunk_count: int
    skipped: bool
    reason: Optional[str] = None
    cancelled: bool = False


def _embed(
    model,
    texts: List[str],
    *,
    batch_size: int = 64,
    on_progress: ProgressFn = _noop,
) -> np.ndarray:
    if not texts:
        return np.zeros((0, DEFAULT_MODEL_DIM), dtype=np.float32)
    out: List[np.ndarray] = []
    total = len(texts)
    i = 0
    on_progress("embed", {"done": 0, "total": total})
    while i < total:
        batch = texts[i : i + batch_size]
        vecs = list(model.embed(batch, batch_size=batch_size))
        out.append(np.asarray(vecs, dtype=np.float32))
        i += len(batch)
        on_progress("embed", {"done": i, "total": total})
    return np.concatenate(out, axis=0)


_MAX_WEBHOOK_CHUNKS = 2_000
_MAX_WEBHOOK_CHUNK_CHARS = 100_000
_MAX_WEBHOOK_TOTAL_CHARS = 4_000_000


def _stream_logical_path(data_dir: Path, source_key: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._@-]+", "_", source_key.strip())[:220].strip("._") or "stream"
    return str((Path(data_dir) / "__minion_stream__" / safe).resolve())


def _payload_digest(source_key: str, chunks_payload: List[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    h.update(source_key.encode("utf-8"))
    h.update(b"\0")
    h.update(json.dumps(chunks_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _resolve_ingest_profile_id(
    conn: sqlite3.Connection,
    profile_id: Optional[str],
) -> str:
    if profile_id:
        return profile_id.strip() or "default"
    return profile_get_active(conn) or "default"


def _source_row_for_path(
    conn: sqlite3.Connection,
    path: str,
    profile_id: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT source_id, sha256, meta_json FROM sources "
        "WHERE path=? AND COALESCE(profile_id, 'default')=?",
        (path, profile_id),
    ).fetchone()


def _source_unchanged(
    conn: sqlite3.Connection,
    path: str,
    digest: str,
    profile_id: str,
) -> bool:
    row = _source_row_for_path(conn, path, profile_id)
    return bool(row and row["sha256"] == digest)


def ingest_webhook_payload(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    source_key: str,
    display_name: Optional[str],
    kind: str,
    parser: str,
    chunks: List[Dict[str, Any]],
    force: bool = False,
    on_progress: ProgressFn = _noop,
    profile_id: Optional[str] = None,
) -> IngestResult:
    """Parse-free ingest: embed JSON chunks (webhook / automation)."""
    ingest_profile = _resolve_ingest_profile_id(conn, profile_id)
    sk = source_key.strip()
    if not sk:
        return IngestResult("", None, "?", "?", 0, True, reason="empty source_key")

    k = (kind or "external").strip()
    if k not in ALL_KINDS:
        return IngestResult("", None, k, parser, 0, True, reason=f"unknown kind {k!r}")
    if k in disabled_kinds():
        return IngestResult("", None, k, parser, 0, True, reason=f"disabled kind {k}")

    if len(chunks) > _MAX_WEBHOOK_CHUNKS:
        return IngestResult("", None, k, parser, 0, True, reason="too many chunks")

    total_chars = 0
    norm_chunks: List[tuple[str, Optional[str], Dict[str, Any]]] = []
    for i, c in enumerate(chunks):
        if not isinstance(c, dict):
            return IngestResult("", None, k, parser, 0, True, reason=f"chunk {i} not an object")
        text = c.get("text")
        if not isinstance(text, str) or not text.strip():
            return IngestResult("", None, k, parser, 0, True, reason=f"chunk {i} missing text")
        if len(text) > _MAX_WEBHOOK_CHUNK_CHARS:
            return IngestResult("", None, k, parser, 0, True, reason=f"chunk {i} too large")
        total_chars += len(text)
        if total_chars > _MAX_WEBHOOK_TOTAL_CHARS:
            return IngestResult("", None, k, parser, 0, True, reason="payload too large")
        role = c.get("role")
        if role is not None and not isinstance(role, str):
            role = str(role)
        meta = c.get("meta") if isinstance(c.get("meta"), dict) else {}
        norm_chunks.append((text, role, meta))

    logical = _stream_logical_path(data_dir, sk)
    digest = _payload_digest(sk, chunks)

    if not force and _source_unchanged(conn, logical, digest, ingest_profile):
        return IngestResult(logical, None, k, parser, 0, True, reason="unchanged")

    on_progress("parse_start", {"suffix": "(webhook)", "bytes": total_chars})
    on_progress("parsed", {"chunks": len(norm_chunks), "kind": k, "parser": parser})

    name = os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [t for t, _, _ in norm_chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    source_meta: Dict[str, Any] = {
        "stream_source_key": sk,
        "display_name": display_name or sk,
        "ingest_route": "webhook",
        "model_name": name,
    }

    source_id = upsert_source(
        conn,
        path=logical,
        kind=k,
        sha256=digest,
        mtime=time.time(),
        bytes_=total_chars,
        parser=parser,
        source_meta=source_meta,
        chunks=norm_chunks,
        embeddings=embeddings,
        profile_id=ingest_profile,
    )

    try:
        telemetry.log_event(
            "ingest",
            path=logical,
            file_kind=k,
            parser=parser,
            chunks=len(norm_chunks),
            skipped=False,
            reason=None,
            result="ingested",
            source_id=source_id,
        )
    except Exception:
        pass

    return IngestResult(
        path=logical,
        source_id=source_id,
        kind=k,
        parser=parser,
        chunk_count=len(norm_chunks),
        skipped=False,
    )


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str] = None,
    force: bool = False,
    on_progress: ProgressFn = _noop,
    cancel_flag: Optional[Dict[str, bool]] = None,
    refresh: bool = False,
    profile_id: Optional[str] = None,
) -> IngestResult:
    """Public ingest entrypoint. Wraps the pipeline with telemetry.

    Telemetry is recorded for every call (success, skip, error) so we have a
    single source of truth for how each file was handled, regardless of
    whether it was triggered by the watcher, the CLI, or `bin/minion add`.
    """
    result = _ingest_file_inner(
        conn,
        path,
        model_name=model_name,
        force=force,
        on_progress=on_progress,
        cancel_flag=cancel_flag,
        refresh=refresh,
        profile_id=profile_id,
    )
    try:
        telemetry.log_event(
            "ingest",
            path=result.path,
            file_kind=result.kind,
            parser=result.parser,
            chunks=result.chunk_count,
            skipped=result.skipped,
            reason=result.reason,
            result=(
                "ingested" if not result.skipped
                else (result.reason.split(":", 1)[0] if result.reason else "skipped")
            ),
            source_id=result.source_id,
        )
    except Exception:
        pass
    return result


def _ingest_file_inner(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str] = None,
    force: bool = False,
    on_progress: ProgressFn = _noop,
    cancel_flag: Optional[Dict[str, bool]] = None,
    refresh: bool = False,
    profile_id: Optional[str] = None,
) -> IngestResult:
    """Parse + embed + upsert. Skips unchanged files (same sha256) unless force=True."""
    ingest_profile = _resolve_ingest_profile_id(conn, profile_id)
    path = Path(path).expanduser().resolve()
    spath = str(path)
    if not path.exists():
        return IngestResult(spath, None, "?", "?", 0, True, reason="missing")

    # Directories are not ingestable in general, but ChatGPT, Claude, Gemini, and Copilot
    # exports ship as a directory of JSON manifests. We sniff content to
    # disambiguate (Claude has `chat_messages`, ChatGPT has a `mapping` tree,
    # Gemini has `messages`/`history`/`turns`, Copilot has `content`/`body`) and dispatch to the right parser.
    if path.is_dir():
        if _looks_like_claude_export(path):
            return _ingest_claude_export_dir(
                conn,
                path,
                model_name=model_name,
                force=force,
                on_progress=on_progress,
                cancel_flag=cancel_flag,
                refresh=refresh,
                profile_id=ingest_profile,
            )
        if _looks_like_chatgpt_export(path):
            return _ingest_chatgpt_export_dir(
                conn,
                path,
                model_name=model_name,
                force=force,
                on_progress=on_progress,
                cancel_flag=cancel_flag,
                refresh=refresh,
                profile_id=ingest_profile,
            )
        if _looks_like_gemini_export(path):
            return _ingest_gemini_export_dir(
                conn,
                path,
                model_name=model_name,
                force=force,
                on_progress=on_progress,
                cancel_flag=cancel_flag,
                refresh=refresh,
                profile_id=ingest_profile,
            )
        if _looks_like_copilot_export(path):
            return _ingest_copilot_export_dir(
                conn,
                path,
                model_name=model_name,
                force=force,
                on_progress=on_progress,
                cancel_flag=cancel_flag,
                refresh=refresh,
                profile_id=ingest_profile,
            )
        return IngestResult(spath, None, "?", "?", 0, True, reason="directory (not a recognized export)")

    # Archives are containers, not parseable files. Unpack in place and let
    # the watcher's next pass ingest each contained file through its proper
    # parser. Keeps the dispatch generic -- no per-vendor assumptions.
    unpacked = _maybe_unpack_archive(path, on_progress=on_progress)
    if unpacked is not None:
        return unpacked

    # Respect user's file-type preferences (settings.json). Skip cleanly so
    # the UI logs it as a deliberate opt-out, not a parser failure.
    if is_disabled_kind(path):
        k = kind_for(path) or path.suffix.lstrip(".") or "?"
        return IngestResult(
            spath, None, k, "disabled", 0, True,
            reason=f"disabled: '{k}' parsing turned off in settings",
        )

    digest = sha256_of_file(path)
    if not force and _source_unchanged(conn, spath, digest, ingest_profile):
        return IngestResult(spath, None, "?", "?", 0, True, reason="unchanged")

    on_progress("parse_start", {"suffix": path.suffix.lower(), "bytes": path.stat().st_size if path.exists() else 0})
    try:
        # Parsers may optionally accept an on_progress kwarg; the dispatcher
        # forwards only the kwargs the target parser actually declares.
        result: ParseResult = parse_file(path, on_progress=on_progress)
    except UnsupportedFile as e:
        return IngestResult(spath, None, "?", "?", 0, True, reason=f"unsupported: {e}")
    except Exception as e:
        # Parsers can raise domain-specific errors (EmptyParse, etc.) with a
        # human-readable reason; preserve that instead of burying it.
        name = type(e).__name__
        msg = str(e) or name
        # Strip our exception class name from common empty-text cases so the
        # UI gets a clean "image-only PDF: ..." rather than "EmptyParse: ...".
        if name in ("EmptyParse", "ValueError"):
            return IngestResult(spath, None, path.suffix.lstrip(".") or "?", "?", 0, True, reason=msg)
        return IngestResult(spath, None, "?", "?", 0, True, reason=f"parse-error: {msg}")

    if not result.chunks:
        return IngestResult(
            spath, None, result.kind, result.parser, 0, True,
            reason="file parsed but produced no text (empty or unsupported content)",
        )

    on_progress("parsed", {"chunks": len(result.chunks), "kind": result.kind, "parser": result.parser})

    name = model_name or os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [c.text for c in result.chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    chunk_tuples = [(c.text, c.role, c.meta) for c in result.chunks]
    stat = path.stat()
    source_meta = dict(result.source_meta or {})
    source_meta.setdefault("suffix", path.suffix.lower())
    source_meta.setdefault("model_name", name)

    source_id = upsert_source(
        conn,
        path=spath,
        kind=result.kind,
        sha256=digest,
        mtime=stat.st_mtime,
        bytes_=stat.st_size,
        parser=result.parser,
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
        profile_id=ingest_profile,
    )

    return IngestResult(
        path=spath,
        source_id=source_id,
        kind=result.kind,
        parser=result.parser,
        chunk_count=len(result.chunks),
        skipped=False,
    )


def _ingest_chatgpt_export_dir(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str],
    force: bool,
    on_progress: ProgressFn,
    cancel_flag: Optional[Dict[str, bool]] = None,
    refresh: bool = False,
    profile_id: str = "default",
) -> IngestResult:
    """Ingest a ChatGPT export directory as a single logical source.

    Shapes accepted:
    - Native OpenAI export root containing `conversations*.json`.
    - Third-party per-conversation exporter with `json/YYYY-MM-DD_*.json`.

    The whole export is represented by one `sources` row keyed by the
    directory path. sha256 is computed over the manifest (relpath, size,
    mtime) so re-running is a no-op unless a manifest changed.

    If refresh=True, only add new conversations (skip already-ingested ones).
    """
    spath = str(path)
    manifests = _chatgpt_export_manifest_paths(path)
    digest = _chatgpt_export_digest(path, manifests)

    # Get existing source metadata for deduplication
    existing_conv_ids: Set[str] = set()
    if refresh or not force:
        row = _source_row_for_path(conn, spath, profile_id)
        if row:
            if row["sha256"] == digest and not force:
                return IngestResult(spath, None, "chatgpt-export", "chatgpt-export", 0, True, reason="unchanged")
            # Extract existing conversation IDs for refresh mode
            try:
                meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
                existing_conv_ids = set(meta.get("conversation_ids", []))
            except (json.JSONDecodeError, TypeError):
                pass

    total_bytes = sum(p.stat().st_size for p in manifests)
    latest_mtime = max((p.stat().st_mtime for p in manifests), default=path.stat().st_mtime)

    on_progress("parse_start", {"suffix": "(dir)", "bytes": total_bytes, "manifests": len(manifests)})
    try:
        # Force the chatgpt_export parser since extension dispatch doesn't
        # apply to directories. Pass existing conversation IDs for deduplication.
        result: ParseResult = parse_file(
            path, 
            parser="parsers.chatgpt_export", 
            on_progress=on_progress, 
            cancel_flag=cancel_flag,
            existing_conv_ids=existing_conv_ids if refresh else None
        )
    except UnsupportedFile as e:
        return IngestResult(spath, None, "chatgpt-export", "?", 0, True, reason=f"unsupported: {e}")
    except Exception as e:
        name = type(e).__name__
        msg = str(e) or name
        # Check if this is a cancellation
        if "cancelled" in str(e).lower():
            return IngestResult(spath, None, "chatgpt-export", "chatgpt-export", 0, True, reason=msg, cancelled=True)
        # Include file path and line number if available from ExportValidationError
        if hasattr(e, 'file_path') and e.file_path:
            reason = f"parse-error: {msg}"
        else:
            reason = f"parse-error: {name}: {msg}"
        return IngestResult(spath, None, "chatgpt-export", "?", 0, True, reason=reason)

    if not result.chunks:
        return IngestResult(
            spath, None, result.kind or "chatgpt-export", result.parser or "chatgpt-export", 0, True,
            reason="export parsed but produced no user-message chunks",
        )

    on_progress("parsed", {"chunks": len(result.chunks), "kind": result.kind, "parser": result.parser})

    name = model_name or os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [c.text for c in result.chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    chunk_tuples = [(c.text, c.role, c.meta) for c in result.chunks]
    source_meta = dict(result.source_meta or {})
    source_meta.setdefault("suffix", "(dir)")
    source_meta.setdefault("model_name", name)
    source_meta.setdefault("manifest_count", len(manifests))
    
    # Track conversation IDs for future deduplication
    conv_ids = set()
    for chunk in result.chunks:
        conv_id = chunk.meta.get("conversation_id")
        if conv_id:
            conv_ids.add(conv_id)
    source_meta["conversation_ids"] = sorted(conv_ids)
    
    # Report deduplication stats if in refresh mode
    if refresh and existing_conv_ids:
        skipped_count = len(existing_conv_ids & conv_ids)
        if skipped_count > 0:
            source_meta["refresh_skipped"] = skipped_count
            on_progress("dedup_stats", {
                "skipped_conversations": skipped_count,
                "total_conversations": len(conv_ids),
                "new_conversations": len(conv_ids) - len(existing_conv_ids & conv_ids)
            })

    source_id = upsert_source(
        conn,
        path=spath,
        kind=result.kind or "chatgpt-export",
        sha256=digest,
        mtime=latest_mtime,
        bytes_=total_bytes,
        parser=result.parser or "chatgpt-export",
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
        profile_id=profile_id,
    )

    return IngestResult(
        path=spath,
        source_id=source_id,
        kind=result.kind or "chatgpt-export",
        parser=result.parser or "chatgpt-export",
        chunk_count=len(result.chunks),
        skipped=False,
    )


def _ingest_claude_export_dir(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str],
    force: bool,
    on_progress: ProgressFn,
    cancel_flag: Optional[Dict[str, bool]] = None,
    refresh: bool = False,
    profile_id: str = "default",
) -> IngestResult:
    """Ingest a Claude.ai export directory as a single logical source.

    One `sources` row keyed by the directory path. sha256 is computed over the
    manifest (relpath, size, mtime) so re-running is a no-op unless a manifest
    changed.

    If refresh=True, only add new conversations (skip already-ingested ones).
    """
    spath = str(path)
    manifests = _claude_export_manifest_paths(path)
    digest = _claude_export_digest(path, manifests)

    # Get existing source metadata for deduplication
    existing_conv_ids: Set[str] = set()
    if refresh or not force:
        row = _source_row_for_path(conn, spath, profile_id)
        if row:
            if row["sha256"] == digest and not force:
                return IngestResult(spath, None, "claude-export", "claude-export", 0, True, reason="unchanged")
            # Extract existing conversation IDs for refresh mode
            try:
                meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
                existing_conv_ids = set(meta.get("conversation_ids", []))
            except (json.JSONDecodeError, TypeError):
                pass

    total_bytes = sum(p.stat().st_size for p in manifests)
    latest_mtime = max((p.stat().st_mtime for p in manifests), default=path.stat().st_mtime)

    on_progress("parse_start", {"suffix": "(dir)", "bytes": total_bytes, "manifests": len(manifests)})
    try:
        result: ParseResult = parse_file(
            path, 
            parser="parsers.claude_export",
            on_progress=on_progress, 
            cancel_flag=cancel_flag,
            existing_conv_ids=existing_conv_ids if refresh else None
        )
    except UnsupportedFile as e:
        return IngestResult(spath, None, "claude-export", "?", 0, True, reason=f"unsupported: {e}")
    except Exception as e:
        name = type(e).__name__
        msg = str(e) or name
        # Check if this is a cancellation
        if "cancelled" in str(e).lower():
            return IngestResult(spath, None, "claude-export", "claude-export", 0, True, reason=msg, cancelled=True)
        return IngestResult(spath, None, "claude-export", "?", 0, True, reason=f"parse-error: {name}: {msg}")

    if not result.chunks:
        return IngestResult(
            spath, None, result.kind or "claude-export", result.parser or "claude-export", 0, True,
            reason="export parsed but produced no user-message chunks",
        )

    on_progress("parsed", {"chunks": len(result.chunks), "kind": result.kind, "parser": result.parser})

    name = model_name or os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [c.text for c in result.chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    chunk_tuples = [(c.text, c.role, c.meta) for c in result.chunks]
    source_meta = dict(result.source_meta or {})
    source_meta.setdefault("suffix", "(dir)")
    source_meta.setdefault("model_name", name)
    source_meta.setdefault("manifest_count", len(manifests))
    
    # Track conversation IDs for future deduplication
    conv_ids = set()
    for chunk in result.chunks:
        conv_id = chunk.meta.get("conversation_id")
        if conv_id:
            conv_ids.add(conv_id)
    source_meta["conversation_ids"] = sorted(conv_ids)
    
    # Report deduplication stats if in refresh mode
    if refresh and existing_conv_ids:
        skipped_count = len(existing_conv_ids & conv_ids)
        if skipped_count > 0:
            source_meta["refresh_skipped"] = skipped_count
            on_progress("dedup_stats", {
                "skipped_conversations": skipped_count,
                "total_conversations": len(conv_ids),
                "new_conversations": len(conv_ids) - len(existing_conv_ids & conv_ids)
            })

    source_id = upsert_source(
        conn,
        path=spath,
        kind=result.kind or "claude-export",
        sha256=digest,
        mtime=latest_mtime,
        bytes_=total_bytes,
        parser=result.parser or "claude-export",
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
        profile_id=profile_id,
    )

    return IngestResult(
        path=spath,
        source_id=source_id,
        kind=result.kind or "claude-export",
        parser=result.parser or "claude-export",
        chunk_count=len(result.chunks),
        skipped=False,
        reason=None,
    )


def _ingest_gemini_export_dir(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str],
    force: bool,
    on_progress: ProgressFn,
    cancel_flag: Optional[Dict[str, bool]] = None,
    refresh: bool = False,
    profile_id: str = "default",
) -> IngestResult:
    """Ingest a Gemini export directory as a single logical source.

    One `sources` row keyed by the directory path. sha256 is computed over the
    manifest (relpath, size, mtime) so re-running is a no-op unless a manifest
    changed.

    If refresh=True, only add new conversations (skip already-ingested ones).
    """
    spath = str(path)
    manifests = _gemini_export_manifest_paths(path)
    digest = _gemini_export_digest(path, manifests)

    # Get existing source metadata for deduplication
    existing_conv_ids: Set[str] = set()
    if refresh or not force:
        row = _source_row_for_path(conn, spath, profile_id)
        if row:
            if row["sha256"] == digest and not force:
                return IngestResult(spath, None, "gemini-export", "gemini-export", 0, True, reason="unchanged")
            # Extract existing conversation IDs for refresh mode
            try:
                meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
                existing_conv_ids = set(meta.get("conversation_ids", []))
            except (json.JSONDecodeError, TypeError):
                pass

    total_bytes = sum(p.stat().st_size for p in manifests)
    latest_mtime = max((p.stat().st_mtime for p in manifests), default=path.stat().st_mtime)

    on_progress("parse_start", {"suffix": "(dir)", "bytes": total_bytes, "manifests": len(manifests)})
    try:
        result: ParseResult = parse_file(
            path, 
            parser="parsers.gemini_export", 
            on_progress=on_progress, 
            cancel_flag=cancel_flag,
            existing_conv_ids=existing_conv_ids if refresh else None
        )
    except UnsupportedFile as e:
        return IngestResult(spath, None, "gemini-export", "?", 0, True, reason=f"unsupported: {e}")
    except Exception as e:
        name = type(e).__name__
        msg = str(e) or name
        # Check if this is a cancellation
        if "cancelled" in str(e).lower():
            return IngestResult(spath, None, "gemini-export", "gemini-export", 0, True, reason=msg, cancelled=True)
        return IngestResult(spath, None, "gemini-export", "?", 0, True, reason=f"parse-error: {name}: {msg}")

    if not result.chunks:
        return IngestResult(
            spath, None, result.kind or "gemini-export", result.parser or "gemini-export", 0, True,
            reason="export parsed but produced no message chunks",
        )

    on_progress("parsed", {"chunks": len(result.chunks), "kind": result.kind, "parser": result.parser})

    name = model_name or os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [c.text for c in result.chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    chunk_tuples = [(c.text, c.role, c.meta) for c in result.chunks]
    source_meta = dict(result.source_meta or {})
    source_meta.setdefault("suffix", "(dir)")
    source_meta.setdefault("model_name", name)
    source_meta.setdefault("manifest_count", len(manifests))
    
    # Track conversation IDs for future deduplication
    conv_ids = set()
    for chunk in result.chunks:
        conv_id = chunk.meta.get("conversation_id")
        if conv_id:
            conv_ids.add(conv_id)
    source_meta["conversation_ids"] = sorted(conv_ids)
    
    # Report deduplication stats if in refresh mode
    if refresh and existing_conv_ids:
        skipped_count = len(existing_conv_ids & conv_ids)
        if skipped_count > 0:
            source_meta["refresh_skipped"] = skipped_count
            on_progress("dedup_stats", {
                "skipped_conversations": skipped_count,
                "total_conversations": len(conv_ids),
                "new_conversations": len(conv_ids) - len(existing_conv_ids & conv_ids)
            })

    source_id = upsert_source(
        conn,
        path=spath,
        kind=result.kind or "gemini-export",
        sha256=digest,
        mtime=latest_mtime,
        bytes_=total_bytes,
        parser=result.parser or "gemini-export",
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
        profile_id=profile_id,
    )

    return IngestResult(
        path=spath,
        source_id=source_id,
        kind=result.kind or "gemini-export",
        parser=result.parser or "gemini-export",
        chunk_count=len(result.chunks),
        skipped=False,
        reason=None,
    )


def _ingest_copilot_export_dir(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model_name: Optional[str],
    force: bool,
    on_progress: ProgressFn,
    cancel_flag: Optional[Dict[str, bool]] = None,
    refresh: bool = False,
    profile_id: str = "default",
) -> IngestResult:
    """Ingest a Copilot export directory as a single logical source.

    One `sources` row keyed by the directory path. sha256 is computed over the
    manifest (relpath, size, mtime) so re-running is a no-op unless a manifest
    changed.

    If refresh=True, only add new conversations (skip already-ingested ones).
    """
    spath = str(path)
    manifests = _copilot_export_manifest_paths(path)
    digest = _copilot_export_digest(path, manifests)

    # Get existing source metadata for deduplication
    existing_conv_ids: Set[str] = set()
    if refresh or not force:
        row = _source_row_for_path(conn, spath, profile_id)
        if row:
            if row["sha256"] == digest and not force:
                return IngestResult(spath, None, "copilot-export", "copilot-export", 0, True, reason="unchanged")
            # Extract existing conversation IDs for refresh mode
            try:
                meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
                existing_conv_ids = set(meta.get("conversation_ids", []))
            except (json.JSONDecodeError, TypeError):
                pass

    total_bytes = sum(p.stat().st_size for p in manifests)
    latest_mtime = max((p.stat().st_mtime for p in manifests), default=path.stat().st_mtime)

    on_progress("parse_start", {"suffix": "(dir)", "bytes": total_bytes, "manifests": len(manifests)})
    try:
        result: ParseResult = parse_file(
            path, 
            parser="parsers.copilot_export", 
            on_progress=on_progress, 
            cancel_flag=cancel_flag,
            existing_conv_ids=existing_conv_ids if refresh else None
        )
    except UnsupportedFile as e:
        return IngestResult(spath, None, "copilot-export", "?", 0, True, reason=f"unsupported: {e}")
    except Exception as e:
        name = type(e).__name__
        msg = str(e) or name
        # Check if this is a cancellation
        if "cancelled" in str(e).lower():
            return IngestResult(spath, None, "copilot-export", "copilot-export", 0, True, reason=msg, cancelled=True)
        return IngestResult(spath, None, "copilot-export", "?", 0, True, reason=f"parse-error: {name}: {msg}")

    if not result.chunks:
        return IngestResult(
            spath, None, result.kind or "copilot-export", result.parser or "copilot-export", 0, True,
            reason="export parsed but produced no message chunks",
        )

    on_progress("parsed", {"chunks": len(result.chunks), "kind": result.kind, "parser": result.parser})

    name = model_name or os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL)
    model = _get_model(name)
    texts = [c.text for c in result.chunks]
    embeddings = _embed(model, texts, on_progress=on_progress)

    chunk_tuples = [(c.text, c.role, c.meta) for c in result.chunks]
    source_meta = dict(result.source_meta or {})
    source_meta.setdefault("suffix", "(dir)")
    source_meta.setdefault("model_name", name)
    source_meta.setdefault("manifest_count", len(manifests))
    
    # Track conversation IDs for future deduplication
    conv_ids = set()
    for chunk in result.chunks:
        conv_id = chunk.meta.get("conversation_id")
        if conv_id:
            conv_ids.add(conv_id)
    source_meta["conversation_ids"] = sorted(conv_ids)
    
    # Report deduplication stats if in refresh mode
    if refresh and existing_conv_ids:
        skipped_count = len(existing_conv_ids & conv_ids)
        if skipped_count > 0:
            source_meta["refresh_skipped"] = skipped_count
            on_progress("dedup_stats", {
                "skipped_conversations": skipped_count,
                "total_conversations": len(conv_ids),
                "new_conversations": len(conv_ids) - len(existing_conv_ids & conv_ids)
            })

    source_id = upsert_source(
        conn,
        path=spath,
        kind=result.kind or "copilot-export",
        sha256=digest,
        mtime=latest_mtime,
        bytes_=total_bytes,
        parser=result.parser or "copilot-export",
        source_meta=source_meta,
        chunks=chunk_tuples,
        embeddings=embeddings,
        profile_id=profile_id,
    )

    return IngestResult(
        path=spath,
        source_id=source_id,
        kind=result.kind or "copilot-export",
        parser=result.parser or "copilot-export",
        chunk_count=len(result.chunks),
        skipped=False,
        reason=None,
    )
