"""Tests for LLM graph corpus mining."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from graph_corpus_mine import (
    graph_mine_enabled,
    graph_mine_status,
    pick_mining_targets,
    run_graph_mine_tick,
)
from store import connect, meta_set, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_pick_mining_targets_includes_me_profile_when_empty(conn) -> None:
    targets = pick_mining_targets(conn, None)
    assert any(t.get("gap_type") == "me_profile" for t in targets)


def test_pick_mining_targets_includes_durable_family_bucket(conn) -> None:
    targets = pick_mining_targets(conn, None)
    assert any(
        t.get("parent_node_id") == "scaffold-people-family" and t.get("mining_kind") == "durable"
        for t in targets
    )


def test_run_mine_tick_skips_without_gemini(conn, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gemini_client.gemini_configured", lambda _dd=None: False)
    out = run_graph_mine_tick(conn, tmp_path, max_llm_calls=2)
    assert out["status"] == "disabled"


def test_run_mine_tick_applies_me_profile(conn, tmp_path: Path, monkeypatch) -> None:
    sec = tmp_path / ".secrets"
    sec.mkdir()
    (sec / "gemini_api_key").write_text("fake", encoding="utf-8")

    fake_hits = {
        "hits": [
            {
                "chunk_id": "ch1",
                "score": 0.6,
                "path": "notes/me.md",
                "text": "Reif grew up in Michigan and works on Minion.",
            }
        ],
        "evidence_refs": ["chunk:ch1"],
    }
    fake_proposal = {
        "confidence": 0.85,
        "actions": [
            {
                "type": "set_me_profile",
                "summary": "Grew up in Michigan; builds Minion.",
                "evidence_refs": ["chunk:ch1"],
            }
        ],
        "unresolved_question": "",
    }

    with patch("forty_two_infer.retrieve_evidence_pack", return_value=fake_hits):
        with patch(
            "forty_two_llm.propose_graph_actions_from_evidence",
            return_value=(fake_proposal, True),
        ):
            out = run_graph_mine_tick(conn, tmp_path, max_llm_calls=1)

    assert out.get("filled", 0) >= 1
    row = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id='scaffold-me'"
    ).fetchone()
    meta = json.loads(row["summary"])
    assert "Michigan" in meta.get("user_profile", "")
    assert meta.get("stability") == "core"


def test_graph_mine_status_tracks_calls(conn, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("graph_corpus_mine.graph_mine_enabled", lambda _dd: True)
    st = graph_mine_status(conn, tmp_path)
    assert st["enabled"] is True
    assert st["max_calls_per_day"] >= 48
