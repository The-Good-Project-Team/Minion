"""Corpus-first 42 graph inference."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from forty_two_infer import (
    AUTO_WRITE_CONFIDENCE,
    build_queries_for_gap,
    retrieve_evidence_pack,
    try_fill_gap_from_corpus,
    validate_proposal,
)
from forty_two_queue import drain_graph_infer_queue, enqueue_graph_infer, has_graph_infer_pending
from graph_fill import open_thread_for_gap, pick_next_gap
from store import connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_build_queries_person_gap() -> None:
    qs = build_queries_for_gap({"gap_type": "person", "label": "Alex"})
    assert "Alex" in qs[0]
    assert any("who is" in q.lower() for q in qs)


def test_validate_rejects_low_confidence() -> None:
    gap = {"gap_type": "person", "subject_id": "gn-x"}
    pack = {"evidence_refs": ["chunk:c1"], "hits": [{"chunk_id": "c1", "score": 0.5}]}
    ok, reason = validate_proposal(
        {"confidence": 0.5, "actions": [{"type": "set_person_summary", "evidence_refs": ["chunk:c1"]}]},
        gap,
        pack,
    )
    assert not ok
    assert reason == "confidence_below_threshold"


def test_validate_accepts_high_confidence_with_citations() -> None:
    gap = {"gap_type": "person", "subject_id": "gn-x"}
    pack = {"evidence_refs": ["chunk:c1"], "hits": [{"chunk_id": "c1", "score": 0.6}]}
    ok, _ = validate_proposal(
        {
            "confidence": AUTO_WRITE_CONFIDENCE,
            "actions": [
                {
                    "type": "set_person_summary",
                    "node_id": "gn-x",
                    "summary": "coworker at Acme",
                    "evidence_refs": ["chunk:c1"],
                }
            ],
        },
        gap,
        pack,
    )
    assert ok


def test_try_fill_skips_without_gemini(conn, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gemini_client.gemini_configured", lambda _dd=None: False)
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    gap = pick_next_gap(conn, None)
    r = try_fill_gap_from_corpus(conn, gap, data_dir=tmp_path)
    assert r["status"] == "skipped"
    assert r["reason"] == "no_gemini"


def test_try_fill_applies_person_summary(conn, tmp_path: Path, monkeypatch) -> None:
    sec = tmp_path / ".secrets"
    sec.mkdir()
    (sec / "gemini_api_key").write_text("fake", encoding="utf-8")
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES('gn-alex', 'person', 'Alex', 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0, '[]', 'vault_local', 1, 1)"
    )
    conn.commit()
    gap = {"gap_type": "person", "subject_id": "gn-alex", "label": "Alex"}
    fake_hits = {
        "hits": [
            {
                "chunk_id": "ch1",
                "score": 0.55,
                "path": "notes/alex.md",
                "text": "Alex is my coworker at Acme",
            }
        ],
        "evidence_refs": ["chunk:ch1", "graph:gn-alex"],
        "query_count": 3,
    }
    proposal = {
        "confidence": 0.88,
        "actions": [
            {
                "type": "set_person_summary",
                "node_id": "gn-alex",
                "summary": "coworker at Acme",
                "evidence_refs": ["chunk:ch1"],
            }
        ],
        "unresolved_question": "",
    }
    with (
        patch("forty_two_infer.retrieve_evidence_pack", return_value=fake_hits),
        patch(
            "forty_two_llm.propose_graph_actions_from_evidence",
            return_value=(proposal, True),
        ),
    ):
        r = try_fill_gap_from_corpus(conn, gap, data_dir=tmp_path)
    assert r["status"] == "filled"
    row = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id='gn-alex'"
    ).fetchone()
    assert row and "coworker" in str(row["summary"]).lower()


def test_open_thread_uses_ambiguous_question(conn, tmp_path: Path, monkeypatch) -> None:
    sec = tmp_path / ".secrets"
    sec.mkdir()
    (sec / "gemini_api_key").write_text("fake", encoding="utf-8")
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    gap = pick_next_gap(conn, None)
    assert gap is not None
    with patch(
        "forty_two_infer.try_fill_gap_from_corpus",
        return_value={
            "status": "needs_question",
            "ambiguous_question": "Is Sam your brother or coworker?",
            "evidence_cite": "notes/a: Sam at work",
        },
    ):
        out = open_thread_for_gap(conn, gap, data_dir=tmp_path)
    assert out.get("thread")
    msgs = out["thread"].get("messages") or []
    assert msgs and "brother or coworker" in msgs[0]["body_md"]


def test_queue_enqueue_and_drain(conn, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("gemini_client.gemini_configured", lambda _dd=None: False)
    enqueue_graph_infer(conn, reason="test")
    assert has_graph_infer_pending(conn)
    r = drain_graph_infer_queue(conn, tmp_path, max_gaps=2)
    assert not has_graph_infer_pending(conn)
    assert "status" in r


def test_scheduler_drains_pending(conn, tmp_path: Path) -> None:
    from forty_two_scheduler import tick

    sec = tmp_path / ".secrets"
    sec.mkdir()
    (sec / "gemini_api_key").write_text("fake", encoding="utf-8")
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    enqueue_graph_infer(conn)
    conn.commit()
    with (
        patch("forty_two_scheduler._notify_chat_updated"),
        patch("forty_two_queue.drain_graph_infer_queue", return_value={"filled": 0, "status": "no_gaps"}),
        patch("graph_fill.open_thread_for_gap") as mock_open,
    ):
        mock_open.return_value = {
            "thread": {"thread_id": "t1"},
            "created": True,
            "gap": {},
        }
        tid = tick(conn, tmp_path)
    assert tid == "t1"
