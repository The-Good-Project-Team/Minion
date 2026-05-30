"""42 Gemini dialogue layer."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from forty_two_llm import (
    MINING_RESPONSE_SCHEMA,
    compose_42_reply,
    iter_42_reply_deltas,
)
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


def test_mining_schema_action_items_are_fully_specified() -> None:
    """Guards the bug where mining action items were declared as a content-free
    ``{"type": "object"}``: Gemini then emitted empty action objects and no graph
    write ever landed. The action item schema must describe ``type`` (with the real
    action enum) and the fields the apply step reads."""
    items = MINING_RESPONSE_SCHEMA["properties"]["actions"]["items"]
    props = items.get("properties") or {}
    # Must not be the empty object-items schema that triggered the regression.
    assert props, "action items must declare properties, not an empty object"
    assert "type" in items.get("required", []), "action 'type' must be required"

    type_enum = set(props["type"].get("enum") or [])
    for action in ("create_node", "add_edge", "set_person_summary", "set_me_profile"):
        assert action in type_enum, f"missing action kind in enum: {action}"

    # The fields apply_proposal actually consumes must be declared so Gemini emits them.
    for field in ("node_kind", "title", "node_id", "from_node_id", "to_node_id", "rel_kind", "summary"):
        assert field in props, f"action item schema missing field: {field}"


def test_compose_falls_back_without_key(conn, tmp_path: Path) -> None:
    old_key = os.environ.pop("GEMINI_API_KEY", None)
    old_minion_key = os.environ.pop("MINION_GEMINI_API_KEY", None)
    old_google_key = os.environ.pop("GOOGLE_API_KEY", None)
    old_base = os.environ.pop("MINION_GEMINI_API_BASE", None)
    old_disable_files = os.environ.get("MINION_GEMINI_DISABLE_SECRET_FILES")
    os.environ["MINION_GEMINI_DISABLE_SECRET_FILES"] = "1"
    try:
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
    finally:
        for key, val in (
            ("GEMINI_API_KEY", old_key),
            ("MINION_GEMINI_API_KEY", old_minion_key),
            ("GOOGLE_API_KEY", old_google_key),
            ("MINION_GEMINI_API_BASE", old_base),
            ("MINION_GEMINI_DISABLE_SECRET_FILES", old_disable_files),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def test_compose_uses_local_gemini_server(conn, tmp_path: Path, fake_gemini) -> None:
    server = fake_gemini("Hey - who's a friend I should add?")
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
    assert used is True
    assert "friend" in body.lower()
    assert server.requests


def test_stream_deltas_from_local_gemini_server(conn, tmp_path: Path, fake_gemini) -> None:
    fake_gemini("Sure - |pick a name.")
    gap = pick_next_gap(conn, None)
    out = open_thread_for_gap(conn, gap)
    result = apply_answer(conn, out["thread"], body="hi there")
    it, flag = iter_42_reply_deltas(
        conn, out["thread"], result, user_text="hi there", data_dir=tmp_path
    )
    text = "".join(it)
    assert flag[0] is True
    assert "pick a name" in text
