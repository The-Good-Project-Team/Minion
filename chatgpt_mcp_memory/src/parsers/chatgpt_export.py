"""ChatGPT export parser.

Accepts either a .zip (OpenAI export) or a directory that already
contains `conversations-*.json`. When given a zip we selectively
extract only the JSON manifests -- ChatGPT exports ship DALL-E images
with 400+ character filenames that blow past macOS's 255-byte limit
(errno 63). We don't index those anyway.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Dict, Any, Optional

from . import ParsedChunk, ParseResult
from ._common import chunk_text, chunk_conversation


# The existing reader lives one dir up; use a path-based import so we don't
# need to package-ify the whole src tree.
_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# We extract every JSON/HTML manifest in the export and skip everything
# else (DALL-E pngs, audio, dalle-generations/, user-*/). Modern exports
# chunk conversations across numbered files (`conversations-1.json`,
# `conversations-YYYY-MM-DD.json`, ...), so matching by extension rather
# than an exact allowlist keeps us forward-compatible.
_EXTRACT_EXTS = (".json", ".html")

# macOS HFS+/APFS caps basenames at 255 bytes; the DALL-E filenames that
# originally broke us are ~400 chars. A conservative guard catches them
# without false positives on any real manifest name.
_MAX_BASENAME_BYTES = 200


class ExportValidationError(Exception):
    """Raised when export structure validation fails."""
    def __init__(self, message: str, file_path: str = None, line_number: int = None):
        self.file_path = file_path
        self.line_number = line_number
        full_message = message
        if file_path:
            full_message = f"{file_path}"
            if line_number:
                full_message += f":{line_number}"
            full_message += f": {message}"
        super().__init__(full_message)


def validate_export_structure(root: Path) -> List[Dict[str, Any]]:
    """Validate ChatGPT export structure before ingestion.
    
    Returns a list of validation errors. Empty list means export is valid.
    Each error is a dict with keys: 'file_path', 'line_number', 'message'.
    """
    errors: List[Dict[str, Any]] = []
    
    try:
        work_dir = _find_export_root(root)
    except FileNotFoundError as e:
        errors.append({
            "file_path": str(root),
            "line_number": None,
            "message": str(e)
        })
        return errors
    
    # Find all manifest files
    manifests = _chatgpt_export_manifest_paths(work_dir)
    if not manifests:
        errors.append({
            "file_path": str(work_dir),
            "line_number": None,
            "message": "No conversation manifests found (expected conversations*.json or json/YYYY-MM-DD_*.json)"
        })
        return errors
    
    # Validate each manifest file
    for manifest_path in manifests:
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError as e:
                    errors.append({
                        "file_path": str(manifest_path),
                        "line_number": e.lineno,
                        "message": f"Invalid JSON: {e.msg}"
                    })
                    continue
                
                # Validate structure based on format
                if isinstance(data, list):
                    # Per-conversation format (json/YYYY-MM-DD_*.json)
                    for idx, conv in enumerate(data):
                        if not isinstance(conv, dict):
                            errors.append({
                                "file_path": str(manifest_path),
                                "line_number": None,
                                "message": f"Conversation at index {idx} is not a dict"
                            })
                            continue
                        if "mapping" not in conv and "current_node" not in conv:
                            errors.append({
                                "file_path": str(manifest_path),
                                "line_number": None,
                                "message": f"Conversation at index {idx} missing required fields (mapping, current_node)"
                            })
                elif isinstance(data, dict):
                    # Native OpenAI format (conversations.json)
                    if "conversations" not in data and not any(key in data for key in ["mapping", "current_node"]):
                        errors.append({
                            "file_path": str(manifest_path),
                            "line_number": None,
                            "message": "Invalid manifest structure: missing 'conversations' list or conversation fields"
                        })
                else:
                    errors.append({
                        "file_path": str(manifest_path),
                        "line_number": None,
                        "message": f"Invalid manifest root type: {type(data).__name__}"
                    })
        except OSError as e:
            errors.append({
                "file_path": str(manifest_path),
                "line_number": None,
                "message": f"Failed to read file: {e}"
            })
    
    return errors


def _chatgpt_export_manifest_paths(root: Path) -> List[Path]:
    """Return the set of JSON files that define this ChatGPT export."""
    native = sorted(root.glob("conversations*.json"))
    if native:
        return native
    per_conv = sorted(root.glob("json/[12][0-9][0-9][0-9]-*.json"))
    return per_conv


def _find_export_root(root: Path) -> Path:
    # Native OpenAI export: conversations*.json at root (or one level down).
    if list(root.glob("conversations*.json")):
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if list(child.glob("conversations*.json")):
            return child
    for p in root.rglob("conversations*.json"):
        return p.parent

    # Per-conversation layout (third-party exporter): `<root>/json/YYYY-MM-DD_*.json`.
    # We return <root> itself; iter_conversation_json_paths knows to look in
    # the `json/` subfolder.
    per_conv = list(root.glob("json/[12][0-9][0-9][0-9]-*.json"))
    if per_conv:
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if list(child.glob("json/[12][0-9][0-9][0-9]-*.json")):
            return child

    raise FileNotFoundError(
        f"No ChatGPT export manifests under {root} "
        "(expected conversations*.json or json/YYYY-MM-DD_*.json)"
    )


def _selective_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """Extract every JSON/HTML manifest. Skip media + overlong names.

    Returns the number of files extracted.
    """
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
            # Defensive: skip any lingering filesystem edge case without
            # aborting the whole extraction.
            continue
    return count


def _noop(_stage: str, _info: dict) -> None:
    pass


def parse(path: Path, *, on_progress=None, validate: bool = True, cancel_flag: Optional[Dict[str, bool]] = None, existing_conv_ids: Optional[set] = None) -> ParseResult:
    from chatgpt_export_reader import (  # type: ignore
        extract_text_from_parts,
        get_linear_path,
        load_conversations_from_export,
        normalize_text,
    )

    emit = on_progress or _noop
    work_dir: Path
    tmp: tempfile.TemporaryDirectory | None = None

    if path.is_dir():
        work_dir = _find_export_root(path)
    elif path.suffix.lower() == ".zip":
        emit("extract_start", {"path": str(path)})
        tmp = tempfile.TemporaryDirectory(prefix="minion_export_")
        with zipfile.ZipFile(path, "r") as zf:
            n = _selective_extract(zf, Path(tmp.name))
        emit("extract_done", {"files": n})
        if n == 0:
            raise ExportValidationError(
                "zip contains no ChatGPT export manifests (expected .json or .html files)",
                file_path=str(path)
            )
        work_dir = _find_export_root(Path(tmp.name))
    else:
        raise ExportValidationError(
            f"Unsupported ChatGPT export path: {path} (expected directory or .zip file)",
            file_path=str(path)
        )

    # Validate export structure if requested
    if validate:
        emit("validate_start", {"dir": str(work_dir)})
        validation_errors = validate_export_structure(work_dir)
        emit("validate_done", {"errors": len(validation_errors)})
        if validation_errors:
            # Raise with the first error for immediate feedback
            first_error = validation_errors[0]
            raise ExportValidationError(
                first_error["message"],
                file_path=first_error["file_path"],
                line_number=first_error["line_number"]
            )

    chunks: List[ParsedChunk] = []
    seq = 0
    parse_errors: List[Dict[str, Any]] = []
    skipped_conversations = 0
    try:
        emit("load_start", {"dir": str(work_dir)})
        conversations = load_conversations_from_export(str(work_dir))
        total_convs = len(conversations)
        emit("load_done", {"conversations": total_convs})

        messages_seen = 0
        start_time = time.time()
        last_progress_time = start_time
        progress_interval = 25  # Emit progress every 25 conversations
        
        for ci, conv in enumerate(conversations):
            # Check for cancellation
            if cancel_flag and cancel_flag.get("cancelled", False):
                emit("parse_cancelled", {
                    "conversations_done": ci,
                    "conversations_total": total_convs,
                    "chunks": len(chunks),
                })
                raise ExportValidationError(
                    "Parsing cancelled by user",
                    file_path=str(path)
                )
            
            try:
                mapping = conv.get("mapping", {}) or {}
                current_node = conv.get("current_node")
                if not mapping or not current_node:
                    continue
                title = conv.get("title") or "(untitled)"
                conv_id = conv.get("id") or conv.get("conversation_id") or "unknown"
                
                # Skip already-ingested conversations in refresh mode
                if existing_conv_ids and conv_id in existing_conv_ids:
                    skipped_conversations += 1
                    continue

                # Collect all messages in this conversation for thread-aware chunking
                conversation_messages: List[Dict[str, Any]] = []
                create_times = []
                for node_id in get_linear_path(mapping, current_node):
                    node = mapping.get(node_id) or {}
                    msg = node.get("message") or {}
                    author = msg.get("author", {}) or {}
                    role = author.get("role")
                    content = msg.get("content") or {}
                    if content.get("content_type") != "text":
                        continue
                    text = normalize_text(extract_text_from_parts(content.get("parts") or []))
                    if not text:
                        continue
                    create_time = msg.get("create_time")
                    if create_time:
                        create_times.append(create_time)
                    conversation_messages.append({
                        "role": role,
                        "text": text,
                        "message_id": str(msg.get("id") or node_id),
                        "create_time": create_time,
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
                    last_progress_time = current_time
            except Exception as e:
                # Track per-conversation parse errors but continue processing
                parse_errors.append({
                    "conversation_index": ci,
                    "conversation_id": conv.get("id") or conv.get("conversation_id") or "unknown",
                    "error": str(e),
                    "error_type": type(e).__name__
                })
                continue
    finally:
        if tmp is not None:
            tmp.cleanup()

    source_meta = {
        "export_root": str(work_dir),
        "roles_indexed": ["user"],
    }
    
    # Include parse errors in metadata if any occurred
    if parse_errors:
        source_meta["parse_errors"] = parse_errors
        source_meta["parse_error_count"] = len(parse_errors)
        # Log to progress callback so UI can display
        emit("parse_errors", {"errors": parse_errors, "total_conversations": total_convs})
    
    # Include deduplication stats if any conversations were skipped
    if skipped_conversations > 0:
        source_meta["skipped_conversations"] = skipped_conversations
        source_meta["total_conversations_in_export"] = total_convs
        source_meta["indexed_conversations"] = total_convs - skipped_conversations

    return ParseResult(
        chunks=chunks,
        source_meta=source_meta,
        kind="chatgpt-export",
        parser="chatgpt-export",
    )
