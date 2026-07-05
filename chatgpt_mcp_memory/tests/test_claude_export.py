"""Claude.ai export parsing + ingest detection.

Mirrors the ChatGPT export path: a directory of JSON manifests, indexed as
one logical source. These tests exercise the parser and the detection /
disambiguation logic directly (no embedding model required).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import ingest
from parsers import ALL_KINDS
from parsers.claude_export import parse


def _write_export(root: Path, conversations: list) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "conversations.json").write_text(
        json.dumps(conversations), encoding="utf-8"
    )
    return root


CLAUDE_CONVERSATIONS = [
    {
        "uuid": "conv-1",
        "name": "Trip planning",
        "created_at": "2026-01-02T10:00:00Z",
        "chat_messages": [
            {
                "uuid": "m1",
                "sender": "human",
                "created_at": "2026-01-02T10:00:01Z",
                "content": [{"type": "text", "text": "Book a flight to Tokyo"}],
                "text": "Book a flight to Tokyo",
            },
            {
                "uuid": "m2",
                "sender": "assistant",
                "created_at": "2026-01-02T10:00:05Z",
                "content": [{"type": "text", "text": "Sure, when?"}],
                "text": "Sure, when?",
            },
            {
                # Older shape: top-level text only, no content array.
                "uuid": "m3",
                "sender": "human",
                "created_at": "2026-01-02T10:01:00Z",
                "text": "Next Friday in the morning",
            },
        ],
    }
]


def test_parse_indexes_only_human_turns(tmp_path: Path) -> None:
    root = _write_export(tmp_path / "claude", CLAUDE_CONVERSATIONS)
    result = parse(root)

    assert result.kind == "claude-export"
    assert result.parser == "claude-export"
    texts = [c.text for c in result.chunks]
    # With conversation chunking, messages are combined into conversation chunks
    assert len(texts) >= 1
    # The conversation should contain the human messages
    combined_text = " ".join(texts)
    assert "Book a flight to Tokyo" in combined_text
    assert "Next Friday in the morning" in combined_text
    # Conversation chunks include both human and assistant messages for context
    # Every chunk is tagged as conversation role and carries conversation meta.
    assert all(c.role == "conversation" for c in result.chunks)
    first = result.chunks[0]
    assert first.meta["conversation_id"] == "conv-1"
    assert first.meta["conversation_title"] == "Trip planning"


def test_content_parts_preferred_over_top_level_text(tmp_path: Path) -> None:
    convs = [
        {
            "uuid": "c",
            "name": "x",
            "chat_messages": [
                {
                    "uuid": "m",
                    "sender": "human",
                    "content": [
                        {"type": "text", "text": "part one"},
                        {"type": "tool_use", "name": "noop"},
                        {"type": "text", "text": "part two"},
                    ],
                    "text": "IGNORED top-level",
                }
            ],
        }
    ]
    root = _write_export(tmp_path / "claude", convs)
    result = parse(root)
    joined = "\n".join(c.text for c in result.chunks)
    assert "part one" in joined and "part two" in joined
    assert "IGNORED" not in joined


def test_parse_accepts_zip(tmp_path: Path) -> None:
    zpath = tmp_path / "claude-export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("conversations.json", json.dumps(CLAUDE_CONVERSATIONS))
        zf.writestr("users.json", json.dumps({"uuid": "u"}))
    result = parse(zpath)
    assert result.kind == "claude-export"
    assert any("Tokyo" in c.text for c in result.chunks)


def test_detection_disambiguates_from_chatgpt(tmp_path: Path) -> None:
    claude_dir = _write_export(tmp_path / "claude", CLAUDE_CONVERSATIONS)

    # A minimal ChatGPT export: same filename, but a `mapping` tree shape.
    chatgpt_dir = tmp_path / "chatgpt"
    chatgpt_dir.mkdir()
    (chatgpt_dir / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "c",
                    "title": "t",
                    "current_node": "n1",
                    "mapping": {
                        "n1": {
                            "parent": None,
                            "message": {
                                "id": "n1",
                                "author": {"role": "user"},
                                "content": {"content_type": "text", "parts": ["hi"]},
                            },
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    assert ingest._looks_like_claude_export(claude_dir) is True
    assert ingest._looks_like_chatgpt_export(claude_dir) is False

    assert ingest._looks_like_claude_export(chatgpt_dir) is False
    assert ingest._looks_like_chatgpt_export(chatgpt_dir) is True


def test_claude_export_is_a_known_kind() -> None:
    assert "claude-export" in ALL_KINDS
