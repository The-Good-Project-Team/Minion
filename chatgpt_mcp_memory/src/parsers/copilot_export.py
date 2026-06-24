"""Copilot export parser.

Accepts either a .zip (Copilot data export) or a directory that already
contains conversation JSON files. Copilot exports can come from GitHub Copilot
or Microsoft Copilot, with varying JSON structures.

The format differs from ChatGPT, Claude, and Gemini: Copilot exports typically
contain conversation files with a structure that includes user and assistant
messages with timestamps and content.
"""
from __future__ import annotations

import json
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import ParsedChunk, ParseResult
from ._common import chunk_text, normalize_text, chunk_conversation


_EXTRACT_EXTS = (".json",)
_MAX_BASENAME_BYTES = 200


def _find_export_root(root: Path) -> Path:
    """Find the root of a Copilot export directory."""
    # Look for common Copilot export patterns
    if list(root.glob("conversations.json")) or list(root.glob("copilot*.json")):
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if list(child.glob("conversations.json")) or list(child.glob("copilot*.json")):
            return child
    for p in root.rglob("conversations.json"):
        return p.parent
    # If no specific pattern found, assume the root itself
    return root


def _selective_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """Extract only JSON files from the zip."""
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
    """Load conversation JSON files from the export directory."""
    conversations: List[Dict[str, Any]] = []
    
    # Try different naming patterns
    paths = (
        sorted(work_dir.glob("conversations.json")) or
        sorted(work_dir.glob("copilot*.json")) or
        sorted(work_dir.glob("conversation*.json")) or
        sorted(work_dir.glob("*.json"))
    )
    
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                conversations.extend(data)
            elif isinstance(data, dict):
                conversations.append(data)
        except (json.JSONDecodeError, IOError):
            continue
    
    return conversations


def _message_text(msg: Dict[str, Any]) -> str:
    """Extract text from a Copilot message."""
    # Handle different Copilot message formats
    if "content" in msg:
        content = msg["content"]
        if isinstance(content, str):
            return normalize_text(content)
        if isinstance(content, list):
            out: List[str] = []
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    text = item["text"]
                    if isinstance(text, str) and text.strip():
                        out.append(text.strip())
                elif isinstance(item, str):
                    if item.strip():
                        out.append(item.strip())
            if out:
                return normalize_text("\n".join(out))
    if "text" in msg:
        text = msg["text"]
        if isinstance(text, str):
            return normalize_text(text)
    if "message" in msg:
        message = msg["message"]
        if isinstance(message, str):
            return normalize_text(message)
    if "body" in msg:
        body = msg["body"]
        if isinstance(body, str):
            return normalize_text(body)
    return ""


def _noop(_stage: str, _info: dict) -> None:
    pass


def parse(path: Path, *, on_progress=None, validate: bool = True, cancel_flag: Optional[Dict[str, bool]] = None, existing_conv_ids: Optional[Set[str]] = None) -> ParseResult:
    emit = on_progress or _noop
    work_dir: Path
    tmp: tempfile.TemporaryDirectory | None = None

    if path.is_dir():
        work_dir = _find_export_root(path)
    elif path.suffix.lower() == ".zip":
        emit("extract_start", {"path": str(path)})
        tmp = tempfile.TemporaryDirectory(prefix="minion_copilot_export_")
        with zipfile.ZipFile(path, "r") as zf:
            n = _selective_extract(zf, Path(tmp.name))
        emit("extract_done", {"files": n})
        if n == 0:
            raise ValueError("zip contains no Copilot export manifests (conversation*.json etc.)")
        work_dir = _find_export_root(Path(tmp.name))
    else:
        raise ValueError(f"Unsupported Copilot export path: {path}")

    chunks: List[ParsedChunk] = []
    seq = 0
    parse_errors: List[Dict[str, Any]] = []
    skipped_conversations = 0
    try:
        emit("load_start", {"dir": str(work_dir)})
        conversations = _load_conversations(work_dir)
        total_convs = len(conversations)
        emit("load_done", {"conversations": total_convs})

        messages_seen = 0
        start_time = time.time()
        progress_interval = 25
        
        for ci, conv in enumerate(conversations):
            # Check for cancellation
            if cancel_flag and cancel_flag.get("cancelled", False):
                emit("parse_cancelled", {
                    "conversations_done": ci,
                    "conversations_total": total_convs,
                    "chunks": len(chunks),
                })
                raise ValueError("Parsing cancelled by user")
            
            # Handle different conversation structures
            messages = []
            if "messages" in conv:
                messages = conv["messages"]
            elif "history" in conv:
                messages = conv["history"]
            elif "turns" in conv:
                messages = conv["turns"]
            elif "conversation" in conv:
                conv_data = conv["conversation"]
                if isinstance(conv_data, dict) and "messages" in conv_data:
                    messages = conv_data["messages"]
            
            if not isinstance(messages, list):
                continue
                
            title = conv.get("title") or conv.get("name") or conv.get("subject") or "(untitled)"
            conv_id = conv.get("id") or conv.get("conversation_id") or conv.get("uuid") or f"conv-{ci}"
            
            # Skip already-ingested conversations in refresh mode
            if existing_conv_ids and conv_id in existing_conv_ids:
                skipped_conversations += 1
                continue

            # Collect all messages in this conversation for thread-aware chunking
            conversation_messages: List[Dict[str, Any]] = []
            create_times = []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                text = _message_text(msg)
                if not text:
                    continue
                
                # Determine role
                role = "user"
                if "author" in msg:
                    author = msg["author"]
                    if isinstance(author, str):
                        role = author.lower()
                    elif isinstance(author, dict):
                        role = author.get("role", "user").lower()
                elif "role" in msg:
                    role = msg["role"].lower()
                elif "sender" in msg:
                    role = msg["sender"].lower()
                elif "from" in msg:
                    from_field = msg["from"]
                    if isinstance(from_field, str):
                        role = from_field.lower()
                    elif isinstance(from_field, dict):
                        role = from_field.get("role", "user").lower()
                
                # Normalize role names
                if role in ("model", "assistant", "ai", "bot", "copilot", "system"):
                    role = "assistant"
                elif role in ("user", "human", "you"):
                    role = "user"
                
                created_at = msg.get("timestamp") or msg.get("created_at") or msg.get("createTime") or msg.get("time")
                if created_at:
                    create_times.append(created_at)
                
                conversation_messages.append({
                    "role": role,
                    "text": text,
                    "message_id": str(msg.get("id") or seq),
                    "create_time": created_at,
                })
            
            if not conversation_messages:
                continue
            
            messages_seen += len(conversation_messages)
            
            # Calculate date range for this conversation
            conversation_start_time = min(create_times) if create_times else None
            conversation_end_time = max(create_times) if create_times else None
            
            # Chunk the conversation while preserving message boundaries
            conversation_chunks = chunk_conversation(conversation_messages)
            for chunk_idx, chunk_text in enumerate(conversation_chunks):
                chunk_meta = {
                    "seq": seq,
                    "conversation_id": str(conv_id),
                    "conversation_title": str(title),
                    "chunk_index": chunk_idx,
                    "total_chunks": len(conversation_chunks),
                    "message_count": len(conversation_messages),
                }
                # Add date range metadata for filtering
                if conversation_start_time:
                    chunk_meta["conversation_start_time"] = conversation_start_time
                if conversation_end_time:
                    chunk_meta["conversation_end_time"] = conversation_end_time
                
                chunks.append(
                    ParsedChunk(
                        text=chunk_text,
                        role="conversation",
                        meta=chunk_meta,
                    )
                )
                seq += 1

            # Emit progress with time estimation
            current_time = time.time()
            if (ci + 1) % progress_interval == 0 or ci + 1 == total_convs:
                elapsed = current_time - start_time
                if ci > 0:
                    avg_time_per_conv = elapsed / (ci + 1)
                    remaining_convs = total_convs - (ci + 1)
                    estimated_remaining = avg_time_per_conv * remaining_convs
                else:
                    estimated_remaining = None
                
                emit(
                    "parse_progress",
                    {
                        "conversations_done": ci + 1,
                        "conversations_total": total_convs,
                        "messages": messages_seen,
                        "chunks": len(chunks),
                        "elapsed_seconds": elapsed,
                        "estimated_remaining_seconds": estimated_remaining,
                        "skipped_conversations": skipped_conversations,
                    },
                )
    finally:
        if tmp is not None:
            tmp.cleanup()

    source_meta = {
        "export_root": str(work_dir),
        "roles_indexed": ["user", "assistant"],
    }
    
    # Include deduplication stats if any conversations were skipped
    if skipped_conversations > 0:
        source_meta["skipped_conversations"] = skipped_conversations
        source_meta["total_conversations_in_export"] = total_convs
        source_meta["indexed_conversations"] = total_convs - skipped_conversations
    
    # Track conversation IDs for future deduplication
    conv_ids = set()
    for chunk in chunks:
        conv_id = chunk.meta.get("conversation_id")
        if conv_id:
            conv_ids.add(conv_id)
    source_meta["conversation_ids"] = sorted(conv_ids)

    return ParseResult(
        chunks=chunks,
        source_meta=source_meta,
        kind="copilot-export",
        parser="copilot-export",
    )
