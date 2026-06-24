"""Tests for Claude export parser improvements."""
import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from parsers.claude_export import parse


def test_parse_conversation_aware_chunking():
    """Test that Claude parser uses conversation-aware chunking."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "uuid": "conv-1",
                "name": "Test conversation",
                "chat_messages": [
                    {
                        "uuid": "msg-1",
                        "sender": "human",
                        "content": [{"type": "text", "text": "Hello, how are you?"}],
                        "created_at": "2024-01-01T00:00:00Z"
                    },
                    {
                        "uuid": "msg-2",
                        "sender": "assistant",
                        "content": [{"type": "text", "text": "I'm doing well, thanks!"}],
                        "created_at": "2024-01-01T00:01:00Z"
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
    """Test that Claude parser includes date range metadata."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "uuid": "conv-1",
                "name": "Test conversation",
                "chat_messages": [
                    {
                        "uuid": "msg-1",
                        "sender": "human",
                        "content": [{"type": "text", "text": "First message"}],
                        "created_at": "2024-01-01T00:00:00Z"
                    },
                    {
                        "uuid": "msg-2",
                        "sender": "human",
                        "content": [{"type": "text", "text": "Second message"}],
                        "created_at": "2024-01-01T01:00:00Z"
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
    """Test that Claude parser skips already-ingested conversations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "uuid": "conv-1",
                "name": "Existing conversation",
                "chat_messages": [
                    {
                        "uuid": "msg-1",
                        "sender": "human",
                        "content": [{"type": "text", "text": "Old message"}],
                        "created_at": "2024-01-01T00:00:00Z"
                    }
                ]
            },
            {
                "uuid": "conv-2",
                "name": "New conversation",
                "chat_messages": [
                    {
                        "uuid": "msg-2",
                        "sender": "human",
                        "content": [{"type": "text", "text": "New message"}],
                        "created_at": "2024-01-02T00:00:00Z"
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
    """Test that Claude parser respects cancellation flag."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        # Create many conversations to test cancellation mid-parse
        conversations = []
        for i in range(50):
            conversations.append({
                "uuid": f"conv-{i}",
                "name": f"Conversation {i}",
                "chat_messages": [
                    {
                        "uuid": f"msg-{i}",
                        "sender": "human",
                        "content": [{"type": "text", "text": f"Message {i}"}],
                        "created_at": "2024-01-01T00:00:00Z"
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
    """Test that Claude parser accepts zip files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        zip_path = tmpdir / "export.zip"
        
        conversations = [
            {
                "uuid": "conv-1",
                "name": "Test conversation",
                "chat_messages": [
                    {
                        "uuid": "msg-1",
                        "sender": "human",
                        "content": [{"type": "text", "text": "Hello"}],
                        "created_at": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("conversations.json", json.dumps(conversations))
        
        result = parse(zip_path)
        
        assert len(result.chunks) > 0
        assert result.kind == "claude-export"


def test_parse_progress_events():
    """Test that Claude parser emits progress events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = []
        for i in range(30):
            conversations.append({
                "uuid": f"conv-{i}",
                "name": f"Conversation {i}",
                "chat_messages": [
                    {
                        "uuid": f"msg-{i}",
                        "sender": "human",
                        "content": [{"type": "text", "text": f"Message {i}"}],
                        "created_at": "2024-01-01T00:00:00Z"
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
    """Test that Claude parser handles different message formats."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        # Test with top-level text instead of content array
        conversations = [
            {
                "uuid": "conv-1",
                "name": "Test conversation",
                "chat_messages": [
                    {
                        "uuid": "msg-1",
                        "sender": "human",
                        "text": "Message with top-level text",
                        "created_at": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "top-level text" in result.chunks[0].text


def test_source_meta_includes_conversation_ids():
    """Test that source metadata includes all conversation IDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "uuid": "conv-1",
                "name": "Conversation 1",
                "chat_messages": [
                    {
                        "uuid": "msg-1",
                        "sender": "human",
                        "content": [{"type": "text", "text": "Message 1"}],
                        "created_at": "2024-01-01T00:00:00Z"
                    }
                ]
            },
            {
                "uuid": "conv-2",
                "name": "Conversation 2",
                "chat_messages": [
                    {
                        "uuid": "msg-2",
                        "sender": "human",
                        "content": [{"type": "text", "text": "Message 2"}],
                        "created_at": "2024-01-02T00:00:00Z"
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
