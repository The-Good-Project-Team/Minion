"""Tests for Gemini export parser."""
import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from parsers.gemini_export import parse


def test_parse_conversation_aware_chunking():
    """Test that Gemini parser uses conversation-aware chunking."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "parts": [{"text": "Hello, how are you?"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    },
                    {
                        "id": "msg-2",
                        "author": "model",
                        "parts": [{"text": "I'm doing well, thanks!"}],
                        "createTime": "2024-01-01T00:01:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        # Check that chunks preserve conversation structure
        for chunk in result.chunks:
            assert "conversation_id" in chunk.meta
            assert chunk.meta["conversation_id"] == "conv-1"
            assert "conversation_title" in chunk.meta
            assert chunk.meta["conversation_title"] == "Test conversation"
            assert "chunk_index" in chunk.meta
            assert "total_chunks" in chunk.meta
            assert "message_count" in chunk.meta


def test_parse_includes_date_range_metadata():
    """Test that Gemini parser includes date range metadata."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "parts": [{"text": "First message"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    },
                    {
                        "id": "msg-2",
                        "author": "user",
                        "parts": [{"text": "Second message"}],
                        "createTime": "2024-01-01T01:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        for chunk in result.chunks:
            assert "conversation_start_time" in chunk.meta
            assert "conversation_end_time" in chunk.meta
            assert chunk.meta["conversation_start_time"] == "2024-01-01T00:00:00Z"
            assert chunk.meta["conversation_end_time"] == "2024-01-01T01:00:00Z"


def test_parse_with_deduplication():
    """Test that Gemini parser skips already-ingested conversations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Existing conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "parts": [{"text": "Old message"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            },
            {
                "id": "conv-2",
                "title": "New conversation",
                "messages": [
                    {
                        "id": "msg-2",
                        "author": "user",
                        "parts": [{"text": "New message"}],
                        "createTime": "2024-01-02T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        # Parse with existing_conv_ids to skip conv-1
        result = parse(tmpdir, existing_conv_ids={"conv-1"})
        
        # Should only have chunks from conv-2
        assert len(result.chunks) > 0
        for chunk in result.chunks:
            assert chunk.meta["conversation_id"] == "conv-2"
        
        # Check source metadata for deduplication stats
        assert "skipped_conversations" in result.source_meta
        assert result.source_meta["skipped_conversations"] == 1


def test_parse_with_cancel_flag():
    """Test that Gemini parser respects cancellation flag."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        # Create many conversations to test cancellation mid-parse
        conversations = []
        for i in range(50):
            conversations.append({
                "id": f"conv-{i}",
                "title": f"Conversation {i}",
                "messages": [
                    {
                        "id": f"msg-{i}",
                        "author": "user",
                        "parts": [{"text": f"Message {i}"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            })
        
        conv_file.write_text(json.dumps(conversations))
        
        cancel_flag = {"cancelled": False}
        
        def cancel_after_5(stage, info):
            if stage == "parse_progress" and info.get("conversations_done", 0) >= 5:
                cancel_flag["cancelled"] = True
        
        with pytest.raises(ValueError, match="cancelled"):
            parse(tmpdir, on_progress=cancel_after_5, cancel_flag=cancel_flag)


def test_parse_accepts_zip():
    """Test that Gemini parser accepts zip files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        zip_path = tmpdir / "export.zip"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "parts": [{"text": "Hello"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("conversations.json", json.dumps(conversations))
        
        result = parse(zip_path)
        
        assert len(result.chunks) > 0
        assert result.kind == "gemini-export"


def test_parse_progress_events():
    """Test that Gemini parser emits progress events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = []
        for i in range(30):
            conversations.append({
                "id": f"conv-{i}",
                "title": f"Conversation {i}",
                "messages": [
                    {
                        "id": f"msg-{i}",
                        "author": "user",
                        "parts": [{"text": f"Message {i}"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            })
        
        conv_file.write_text(json.dumps(conversations))
        
        progress_events = []
        
        def collect_progress(stage, info):
            progress_events.append((stage, info))
        
        result = parse(tmpdir, on_progress=collect_progress)
        
        # Check for expected progress events
        stages = [stage for stage, _ in progress_events]
        assert "extract_start" not in stages  # Not a zip
        assert "load_start" in stages
        assert "load_done" in stages
        assert "parse_progress" in stages
        
        # Check parse_progress includes time estimation
        progress_info = [info for stage, info in progress_events if stage == "parse_progress"]
        assert any("elapsed_seconds" in info for info in progress_info)
        assert any("estimated_remaining_seconds" in info for info in progress_info)


def test_parse_with_different_message_formats():
    """Test that Gemini parser handles different message formats."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        # Test with history instead of messages
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "history": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "parts": [{"text": "Message in history"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "history" in result.chunks[0].text


def test_parse_with_turns_format():
    """Test that Gemini parser handles turns format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "turns": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "parts": [{"text": "Message in turns"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "turns" in result.chunks[0].text


def test_source_meta_includes_conversation_ids():
    """Test that source metadata includes all conversation IDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Conversation 1",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "parts": [{"text": "Message 1"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            },
            {
                "id": "conv-2",
                "title": "Conversation 2",
                "messages": [
                    {
                        "id": "msg-2",
                        "author": "user",
                        "parts": [{"text": "Message 2"}],
                        "createTime": "2024-01-02T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert "conversation_ids" in result.source_meta
        conv_ids = result.source_meta["conversation_ids"]
        assert "conv-1" in conv_ids
        assert "conv-2" in conv_ids


def test_parse_with_top_level_text():
    """Test that Gemini parser handles top-level text field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "text": "Message with top-level text",
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "top-level text" in result.chunks[0].text


def test_parse_with_content_field():
    """Test that Gemini parser handles content field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "content": "Message with content field",
                        "createTime": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "content field" in result.chunks[0].text


def test_parse_normalizes_role_names():
    """Test that Gemini parser normalizes role names."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "parts": [{"text": "User message"}],
                        "createTime": "2024-01-01T00:00:00Z"
                    },
                    {
                        "id": "msg-2",
                        "author": "model",
                        "parts": [{"text": "Model message"}],
                        "createTime": "2024-01-01T00:01:00Z"
                    },
                    {
                        "id": "msg-3",
                        "author": "ai",
                        "parts": [{"text": "AI message"}],
                        "createTime": "2024-01-01T00:02:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        # Check that roles are normalized to user/assistant
        for chunk in result.chunks:
            # The chunk text should contain role prefixes
            assert "user" in chunk.text.lower() or "assistant" in chunk.text.lower()
