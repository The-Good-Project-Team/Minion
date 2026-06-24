"""Tests for Copilot export parser."""
import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from parsers.copilot_export import parse


def test_parse_conversation_aware_chunking():
    """Test that Copilot parser uses conversation-aware chunking."""
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
                        "content": "Hello, how are you?",
                        "timestamp": "2024-01-01T00:00:00Z"
                    },
                    {
                        "id": "msg-2",
                        "author": "copilot",
                        "content": "I'm doing well, thanks!",
                        "timestamp": "2024-01-01T00:01:00Z"
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
    """Test that Copilot parser includes date range metadata."""
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
                        "content": "First message",
                        "timestamp": "2024-01-01T00:00:00Z"
                    },
                    {
                        "id": "msg-2",
                        "author": "user",
                        "content": "Second message",
                        "timestamp": "2024-01-01T01:00:00Z"
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
    """Test that Copilot parser skips already-ingested conversations."""
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
                        "content": "Old message",
                        "timestamp": "2024-01-01T00:00:00Z"
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
                        "content": "New message",
                        "timestamp": "2024-01-02T00:00:00Z"
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
    """Test that Copilot parser respects cancellation flag."""
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
                        "content": f"Message {i}",
                        "timestamp": "2024-01-01T00:00:00Z"
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
    """Test that Copilot parser accepts zip files."""
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
                        "content": "Hello",
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("conversations.json", json.dumps(conversations))
        
        result = parse(zip_path)
        
        assert len(result.chunks) > 0
        assert result.kind == "copilot-export"


def test_parse_progress_events():
    """Test that Copilot parser emits progress events."""
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
                        "content": f"Message {i}",
                        "timestamp": "2024-01-01T00:00:00Z"
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
    """Test that Copilot parser handles different message formats."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        # Test with body field instead of content
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "body": "Message with body field",
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "body field" in result.chunks[0].text


def test_parse_with_history_format():
    """Test that Copilot parser handles history format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "history": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "content": "Message in history",
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "history" in result.chunks[0].text


def test_parse_with_turns_format():
    """Test that Copilot parser handles turns format."""
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
                        "content": "Message in turns",
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "turns" in result.chunks[0].text


def test_parse_with_nested_conversation():
    """Test that Copilot parser handles nested conversation structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "conversation": {
                    "messages": [
                        {
                            "id": "msg-1",
                            "author": "user",
                            "content": "Message in nested conversation",
                            "timestamp": "2024-01-01T00:00:00Z"
                        }
                    ]
                }
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "nested conversation" in result.chunks[0].text


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
                        "content": "Message 1",
                        "timestamp": "2024-01-01T00:00:00Z"
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
                        "content": "Message 2",
                        "timestamp": "2024-01-02T00:00:00Z"
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


def test_parse_with_text_field():
    """Test that Copilot parser handles text field."""
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
                        "text": "Message with text field",
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "text field" in result.chunks[0].text


def test_parse_with_message_field():
    """Test that Copilot parser handles message field."""
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
                        "message": "Message with message field",
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "message field" in result.chunks[0].text


def test_parse_normalizes_role_names():
    """Test that Copilot parser normalizes role names."""
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
                        "content": "User message",
                        "timestamp": "2024-01-01T00:00:00Z"
                    },
                    {
                        "id": "msg-2",
                        "author": "copilot",
                        "content": "Copilot message",
                        "timestamp": "2024-01-01T00:01:00Z"
                    },
                    {
                        "id": "msg-3",
                        "author": "system",
                        "content": "System message",
                        "timestamp": "2024-01-01T00:02:00Z"
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


def test_parse_with_from_field():
    """Test that Copilot parser handles from field."""
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
                        "from": {"role": "user"},
                        "content": "Message with from field",
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "from field" in result.chunks[0].text


def test_parse_with_content_array():
    """Test that Copilot parser handles content array."""
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
                        "content": [
                            {"text": "First part"},
                            {"text": "Second part"}
                        ],
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert "First part" in result.chunks[0].text
        assert "Second part" in result.chunks[0].text


def test_parse_with_copilot_specific_file():
    """Test that Copilot parser handles copilot-specific file names."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        conv_file = tmpdir / "copilot_conversations.json"
        
        conversations = [
            {
                "id": "conv-1",
                "title": "Test conversation",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": "user",
                        "content": "Message",
                        "timestamp": "2024-01-01T00:00:00Z"
                    }
                ]
            }
        ]
        
        conv_file.write_text(json.dumps(conversations))
        
        result = parse(tmpdir)
        
        assert len(result.chunks) > 0
        assert result.kind == "copilot-export"
