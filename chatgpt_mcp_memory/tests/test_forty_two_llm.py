"""42 Gemini dialogue layer."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from forty_two_llm import compose_42_reply, iter_42_reply_deltas
from graph_fill import apply_answer, open_thread_for_gap, pick_next_gap
from store import connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_compose_falls_back_without_key(conn, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gemini_client.gemini_configured", lambda _dd=None: False)
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    gap = pick_next_gap(conn, None)
    out = open_thread_for_gap(conn, gap)
    result = apply_answer(conn, out["thread"], body="hi there")
    body, used = compose_42_reply(
        conn, out["thread"], result, user_text="hi there", data_dir=tmp_path
    )
    assert used is False
    assert "doesn't look like a name" in body or "42" in body.lower()


def test_compose_uses_gemini_when_mocked(conn, tmp_path: Path) -> None:
    os.environ["GEMINI_API_KEY"] = "test-key"
    try:
        gap = pick_next_gap(conn, None)
        out = open_thread_for_gap(conn, gap)
        result = apply_answer(conn, out["thread"], body="hi there")
        with patch(
            "gemini_client.gemini_chat",
            return_value="Hey — who's a friend I should add?",
        ):
            body, used = compose_42_reply(
                conn, out["thread"], result, user_text="hi there", data_dir=tmp_path
            )
        assert used is True
        assert "friend" in body.lower()
    finally:
        os.environ.pop("GEMINI_API_KEY", None)


def test_stream_deltas_mock(conn, tmp_path: Path) -> None:
    os.environ["GEMINI_API_KEY"] = "test-key"
    try:
        gap = pick_next_gap(conn, None)
        out = open_thread_for_gap(conn, gap)
        result = apply_answer(conn, out["thread"], body="hi there")

        def fake_stream(**_kwargs):
            yield "Sure — "
            yield "pick a name."

        with patch("gemini_client.gemini_chat_stream", fake_stream):
            it, flag = iter_42_reply_deltas(
                conn, out["thread"], result, user_text="hi there", data_dir=tmp_path
            )
            text = "".join(it)
        assert flag[0] is True
        assert "pick a name" in text
    finally:
        os.environ.pop("GEMINI_API_KEY", None)
