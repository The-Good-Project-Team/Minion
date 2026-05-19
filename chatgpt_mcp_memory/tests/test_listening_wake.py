"""Wake phrase detection in listening ingest."""
from __future__ import annotations

from listening_ingest import _wake_word_in_text


def test_wake_word_boundary() -> None:
    assert _wake_word_in_text("Hey Minion, remind me")
    assert _wake_word_in_text("MINION please")
    assert not _wake_word_in_text("dominion")
    assert not _wake_word_in_text("")
