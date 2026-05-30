"""Claude.ai export parser.

Accepts either a .zip (Claude data export) or a directory that already
contains `conversations.json`. Claude exports are small JSON manifests
(`conversations.json`, `projects.json`, `users.json`); we extract only the
JSON and ignore anything else.

Shape differs from ChatGPT: a Claude export is a flat list of conversations,
each with a `chat_messages` list. Every message carries a `sender`
("human" / "assistant") and either a top-level `text` or a `content` array
of typed parts. We index the human turns, mirroring the ChatGPT parser.
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from . import ParsedChunk, ParseResult
from ._common import chunk_text, normalize_text


# Claude exports only ship JSON manifests; keep the same selective-extract
# guard the ChatGPT parser uses so we never trip over odd filenames.
_EXTRACT_EXTS = (".json",)
_MAX_BASENAME_BYTES = 200


def _find_export_root(root: Path) -> Path:
    if list(root.glob("conversations.json")) or list(root.glob("conversations-*.json")):
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if list(child.glob("conversations.json")) or list(child.glob("conversations-*.json")):
            return child
    for p in root.rglob("conversations.json"):
        return p.parent
    raise FileNotFoundError(
        f"No Claude export manifest under {root} (expected conversations.json)"
    )


def _selective_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    count = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        basename = Path(info.filename).name
        if not basename.lower().endswith(_EXTRACT_EXTS):
            continue
        if len(basename.encode("utf-8", "replace")) > _MAX_BASENAME_BYTES:
            continue
        target = dest / info.filename
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            count += 1
        except OSError:
            continue
    return count


def _load_conversations(work_dir: Path) -> List[Dict[str, Any]]:
    paths = sorted(work_dir.glob("conversations.json")) or sorted(
        work_dir.glob("conversations-*.json")
    )
    conversations: List[Dict[str, Any]] = []
    for p in paths:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            conversations.extend(data)
        elif isinstance(data, dict):
            conversations.append(data)
    return conversations


def _message_text(msg: Dict[str, Any]) -> str:
    """Prefer the structured `content` parts; fall back to top-level `text`."""
    parts = msg.get("content")
    if isinstance(parts, list):
        out: List[str] = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
        if out:
            return normalize_text("\n".join(out))
    text = msg.get("text")
    if isinstance(text, str):
        return normalize_text(text)
    return ""


def _noop(_stage: str, _info: dict) -> None:
    pass


def parse(path: Path, *, on_progress=None) -> ParseResult:
    emit = on_progress or _noop
    work_dir: Path
    tmp: tempfile.TemporaryDirectory | None = None

    if path.is_dir():
        work_dir = _find_export_root(path)
    elif path.suffix.lower() == ".zip":
        emit("extract_start", {"path": str(path)})
        tmp = tempfile.TemporaryDirectory(prefix="minion_claude_export_")
        with zipfile.ZipFile(path, "r") as zf:
            n = _selective_extract(zf, Path(tmp.name))
        emit("extract_done", {"files": n})
        if n == 0:
            raise ValueError("zip contains no Claude export manifests (conversations.json etc.)")
        work_dir = _find_export_root(Path(tmp.name))
    else:
        raise ValueError(f"Unsupported Claude export path: {path}")

    chunks: List[ParsedChunk] = []
    seq = 0
    try:
        emit("load_start", {"dir": str(work_dir)})
        conversations = _load_conversations(work_dir)
        total_convs = len(conversations)
        emit("load_done", {"conversations": total_convs})

        messages_seen = 0
        for ci, conv in enumerate(conversations):
            messages = conv.get("chat_messages") or []
            if not isinstance(messages, list):
                continue
            title = conv.get("name") or "(untitled)"
            conv_id = conv.get("uuid") or conv.get("conversation_id") or "unknown"

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if msg.get("sender") != "human":
                    continue
                text = _message_text(msg)
                if not text:
                    continue
                messages_seen += 1
                for t in chunk_text(text):
                    chunks.append(
                        ParsedChunk(
                            text=t,
                            role="user",
                            meta={
                                "seq": seq,
                                "conversation_id": str(conv_id),
                                "conversation_title": str(title),
                                "create_time": msg.get("created_at"),
                                "message_id": str(msg.get("uuid") or seq),
                            },
                        )
                    )
                    seq += 1

            if (ci + 1) % 25 == 0 or ci + 1 == total_convs:
                emit(
                    "parse_progress",
                    {
                        "conversations_done": ci + 1,
                        "conversations_total": total_convs,
                        "messages": messages_seen,
                        "chunks": len(chunks),
                    },
                )
    finally:
        if tmp is not None:
            tmp.cleanup()

    return ParseResult(
        chunks=chunks,
        source_meta={"export_root": str(work_dir), "roles_indexed": ["user"]},
        kind="claude-export",
        parser="claude-export",
    )
