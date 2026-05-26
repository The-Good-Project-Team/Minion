"""Corpus summary formatting."""
from __future__ import annotations

from corpus_context import corpus_summary_line


def test_corpus_summary_uses_basename() -> None:
    line = corpus_summary_line(
        {
            "hits": [
                {
                    "path": "/Users/me/Library/Application Support/Minion/data/inbox/foo.json",
                    "text": "hi there",
                }
            ]
        }
    )
    assert "inbox/foo.json" in line or "foo.json" in line
    assert "/Users/me/Library" not in line
