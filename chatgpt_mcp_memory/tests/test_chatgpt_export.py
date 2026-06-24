"""ChatGPT export parsing + ingest verification.

Tests for error handling, progress tracking, deduplication, and conversation-aware chunking.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from parsers.chatgpt_export import parse, validate_export_structure, ExportValidationError


def _write_export(root: Path, conversations: list) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "conversations.json").write_text(
        json.dumps(conversations), encoding="utf-8"
    )
    return root


CHATGPT_CONVERSATIONS = [
    {
        "id": "conv-1",
        "title": "Trip planning",
        "current_node": "m1",
        "mapping": {
            "m1": {
                "parent": None,
                "message": {
                    "id": "m1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Book a flight to Tokyo"]},
                    "create_time": 1704206401.0,
                },
            },
            "m2": {
                "parent": "m1",
                "message": {
                    "id": "m2",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["Sure, when?"]},
                    "create_time": 1704206405.0,
                },
            },
            "m3": {
                "parent": "m2",
                "message": {
                    "id": "m3",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Next Friday in the morning"]},
                    "create_time": 1704206460.0,
                },
            },
        },
    }
]


def test_parse_conversation_aware_chunking(tmp_path: Path) -> None:
    """Test that conversations are chunked with message boundaries preserved."""
    root = _write_export(tmp_path / "chatgpt", CHATGPT_CONVERSATIONS)
    result = parse(root)

    assert result.kind == "chatgpt-export"
    assert result.parser == "chatgpt-export"
    assert len(result.chunks) > 0
    
    # Check that chunks have conversation metadata
    first_chunk = result.chunks[0]
    assert first_chunk.role == "conversation"
    assert first_chunk.meta["conversation_id"] == "conv-1"
    assert first_chunk.meta["conversation_title"] == "Trip planning"
    assert "message_count" in first_chunk.meta
    assert "chunk_index" in first_chunk.meta
    assert "total_chunks" in first_chunk.meta


def test_parse_includes_date_range_metadata(tmp_path: Path) -> None:
    """Test that conversation date ranges are included in chunk metadata."""
    root = _write_export(tmp_path / "chatgpt", CHATGPT_CONVERSATIONS)
    result = parse(root)

    first_chunk = result.chunks[0]
    assert "conversation_start_time" in first_chunk.meta
    assert "conversation_end_time" in first_chunk.meta
    # The date range should be calculated from the conversation messages
    assert first_chunk.meta["conversation_start_time"] is not None
    assert first_chunk.meta["conversation_end_time"] is not None


def test_validate_export_structure_valid(tmp_path: Path) -> None:
    """Test validation of a valid ChatGPT export."""
    root = _write_export(tmp_path / "chatgpt", CHATGPT_CONVERSATIONS)
    errors = validate_export_structure(root)
    assert len(errors) == 0


def test_validate_export_structure_invalid_json(tmp_path: Path) -> None:
    """Test validation detects malformed JSON."""
    root = tmp_path / "chatgpt"
    root.mkdir(parents=True, exist_ok=True)
    (root / "conversations.json").write_text("{invalid json", encoding="utf-8")
    
    errors = validate_export_structure(root)
    assert len(errors) > 0
    assert any("Invalid JSON" in e["message"] for e in errors)


def test_validate_export_structure_missing_manifest(tmp_path: Path) -> None:
    """Test validation detects missing manifest files."""
    root = tmp_path / "chatgpt"
    root.mkdir(parents=True, exist_ok=True)
    
    errors = validate_export_structure(root)
    assert len(errors) > 0
    # The error message should mention missing manifests or no export structure
    assert any("manifest" in e["message"].lower() or "conversation" in e["message"].lower() for e in errors)


def test_validate_export_structure_missing_required_fields(tmp_path: Path) -> None:
    """Test validation detects missing required conversation fields."""
    root = tmp_path / "chatgpt"
    root.mkdir(parents=True, exist_ok=True)
    (root / "conversations.json").write_text(
        json.dumps([{"id": "c"}]), encoding="utf-8"
    )
    
    errors = validate_export_structure(root)
    assert len(errors) > 0
    assert any("missing required fields" in e["message"] for e in errors)


def test_parse_with_deduplication(tmp_path: Path) -> None:
    """Test that existing conversation IDs are skipped in refresh mode."""
    root = _write_export(tmp_path / "chatgpt", CHATGPT_CONVERSATIONS)
    
    # First parse without deduplication
    result1 = parse(root, existing_conv_ids=None)
    conv_ids = {c.meta["conversation_id"] for c in result1.chunks}
    assert len(conv_ids) > 0
    
    # Second parse with deduplication (should skip all)
    result2 = parse(root, existing_conv_ids=conv_ids)
    assert len(result2.chunks) == 0


def test_parse_with_partial_deduplication(tmp_path: Path) -> None:
    """Test that only specified conversation IDs are skipped."""
    root = _write_export(tmp_path / "chatgpt", CHATGPT_CONVERSATIONS)
    
    # Skip a non-existent ID
    result = parse(root, existing_conv_ids={"non-existent-id"})
    assert len(result.chunks) > 0


def test_parse_with_cancel_flag(tmp_path: Path) -> None:
    """Test that parsing can be cancelled via cancel flag."""
    root = _write_export(tmp_path / "chatgpt", CHATGPT_CONVERSATIONS)
    
    cancel_flag = {"cancelled": True}
    try:
        parse(root, cancel_flag=cancel_flag)
        assert False, "Should have raised ExportValidationError"
    except ExportValidationError as e:
        assert "cancelled" in str(e).lower()


def test_parse_accepts_zip(tmp_path: Path) -> None:
    """Test that zip exports are accepted and parsed."""
    zpath = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("conversations.json", json.dumps(CHATGPT_CONVERSATIONS))
        zf.writestr("config.json", json.dumps({"version": "1"}))
    
    result = parse(zpath)
    assert result.kind == "chatgpt-export"
    assert any("Tokyo" in c.text for c in result.chunks)


def test_parse_progress_events(tmp_path: Path) -> None:
    """Test that progress events are emitted during parsing."""
    root = _write_export(tmp_path / "chatgpt", CHATGPT_CONVERSATIONS)
    
    events = []
    def on_progress(stage: str, info: dict) -> None:
        events.append((stage, info))
    
    parse(root, on_progress=on_progress)
    
    # Check that key events were emitted
    event_types = [e[0] for e in events]
    assert "load_start" in event_types
    assert "load_done" in event_types
    assert "parse_progress" in event_types


def test_parse_with_validation_disabled(tmp_path: Path) -> None:
    """Test that validation can be disabled for faster parsing."""
    root = _write_export(tmp_path / "chatgpt", CHATGPT_CONVERSATIONS)
    
    result = parse(root, validate=False)
    assert result.kind == "chatgpt-export"
    assert len(result.chunks) > 0


def test_parse_per_conversation_format(tmp_path: Path) -> None:
    """Test parsing of per-conversation format (json/YYYY-MM-DD_*.json)."""
    root = tmp_path / "chatgpt" / "json"
    root.mkdir(parents=True, exist_ok=True)
    
    (root / "2026-01-02_conv1.json").write_text(
        json.dumps(CHATGPT_CONVERSATIONS), encoding="utf-8"
    )
    
    result = parse(root.parent)
    assert result.kind == "chatgpt-export"
    assert len(result.chunks) > 0
