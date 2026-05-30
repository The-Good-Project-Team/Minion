"""Corpus-first 42 graph inference."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ingest import _embed, _get_model
from forty_two_infer import (
    AUTO_WRITE_CONFIDENCE,
    build_queries_for_gap,
    retrieve_evidence_pack,
    try_fill_gap_from_corpus,
    validate_proposal,
)
from forty_two_queue import drain_graph_infer_queue, enqueue_graph_infer, has_graph_infer_pending
from graph_fill import open_thread_for_gap, pick_next_gap
from store import connect, seed_sync_sources, upsert_source


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


def _without_gemini_env():
    saved = {
        key: os.environ.pop(key, None)
        for key in (
            "GEMINI_API_KEY",
            "MINION_GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "MINION_GEMINI_API_BASE",
            "MINION_GEMINI_DISABLE_SECRET_FILES",
        )
    }
    os.environ["MINION_GEMINI_DISABLE_SECRET_FILES"] = "1"
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _insert_person(conn, node_id: str, title: str) -> None:
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0, '[]', 'vault_local', 1, 1)",
        (node_id, title),
    )


def _add_text_source(conn, tmp_path: Path, name: str, text: str) -> None:
    os.environ["MINION_DETERMINISTIC_EMBEDDINGS"] = "1"
    model = _get_model("deterministic")
    upsert_source(
        conn,
        path=str(tmp_path / "notes" / f"{name}.md"),
        kind="text",
        sha256=f"{name}-sha",
        mtime=1.0,
        bytes_=len(text),
        parser="test",
        source_meta={},
        chunks=[(text, "user", {})],
        embeddings=_embed(model, [text]),
    )


def test_try_fill_skips_without_gemini(conn, tmp_path: Path) -> None:
    saved = _without_gemini_env()
    conn.execute("DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')")
    conn.commit()
    try:
        gap = pick_next_gap(conn, None)
        r = try_fill_gap_from_corpus(conn, gap, data_dir=tmp_path)
        assert r["status"] == "skipped"
        assert r["reason"] == "no_gemini"
    finally:
        _restore_env(saved)


def test_try_fill_applies_person_summary(conn, tmp_path: Path, fake_gemini) -> None:
    _insert_person(conn, "gn-alex", "Alex")
    _add_text_source(conn, tmp_path, "alex", "Alex is my coworker at Acme.")
    conn.commit()
    gap = {"gap_type": "person", "subject_id": "gn-alex", "label": "Alex"}
    proposal = {
        "confidence": 0.88,
        "actions": [
            {
                "type": "set_person_summary",
                "node_id": "gn-alex",
                "summary": "coworker at Acme",
            }
        ],
        "unresolved_question": "",
    }
    fake_gemini(json.dumps(proposal))
    try:
        r = try_fill_gap_from_corpus(conn, gap, data_dir=tmp_path)
    finally:
        os.environ.pop("MINION_DETERMINISTIC_EMBEDDINGS", None)
    assert r["status"] == "filled"
    row = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id='gn-alex'"
    ).fetchone()
    assert row and "coworker" in str(row["summary"]).lower()


def test_open_thread_uses_ambiguous_question(conn, tmp_path: Path, fake_gemini) -> None:
    _insert_person(conn, "gn-sam", "Sam")
    _add_text_source(conn, tmp_path, "sam", "Sam may be my brother or coworker.")
    conn.commit()
    fake_gemini(
        json.dumps(
            {
                "confidence": 0.8,
                "actions": [],
                "unresolved_question": "Is Sam your brother or coworker?",
            }
        )
    )
    gap = {"gap_type": "person", "subject_id": "gn-sam", "label": "Sam"}
    try:
        out = open_thread_for_gap(conn, gap, data_dir=tmp_path)
    finally:
        os.environ.pop("MINION_DETERMINISTIC_EMBEDDINGS", None)
    assert out.get("thread")
    msgs = out["thread"].get("messages") or []
    assert msgs and "brother or coworker" in msgs[0]["body_md"]


def test_apply_proposal_resolves_loose_edge_endpoints(conn, tmp_path: Path) -> None:
    """A create_node + add_edge proposal that references the new node by a loose,
    LLM-invented id ("practice-of-life") and "me" must land the node AND connect a
    real, non-dangling edge scaffold-me -> <real node id>."""
    from forty_two_infer import apply_proposal

    gap = {
        "gap_type": "bucket",
        "parent_node_id": "scaffold-work-companies",
        "node_kind": "organization",
        "bucket_label": "Companies",
    }
    proposal = {
        "confidence": 0.9,
        "actions": [
            {
                "type": "create_node",
                "node_kind": "organization",
                "title": "Practice of Life",
                "node_id": "practice-of-life",  # loose id the model made up
                "parent_node_id": "scaffold-work-companies",
                "summary": "health coaching company",
            },
            {
                "type": "add_edge",
                "from_node_id": "me",  # alias -> scaffold-me
                "to_node_id": "practice-of-life",  # loose id -> resolved to real node
                "rel_kind": "works_at",
            },
        ],
    }
    out = apply_proposal(conn, gap, proposal, data_dir=tmp_path)
    conn.commit()
    assert out["filled"]

    node = conn.execute(
        "SELECT node_id FROM graph_nodes WHERE title='Practice of Life'"
    ).fetchone()
    assert node is not None
    real_id = node["node_id"]
    assert real_id != "practice-of-life"  # got a real generated id

    edge = conn.execute(
        "SELECT from_node_id, to_node_id FROM graph_edges WHERE rel_kind='works_at'"
    ).fetchone()
    assert edge is not None
    assert edge["from_node_id"] == "scaffold-me"
    assert edge["to_node_id"] == real_id

    # No edge anywhere may point at a node that does not exist.
    dangling = conn.execute(
        "SELECT COUNT(*) FROM graph_edges e "
        "WHERE NOT EXISTS (SELECT 1 FROM graph_nodes n WHERE n.node_id=e.from_node_id) "
        "   OR NOT EXISTS (SELECT 1 FROM graph_nodes n WHERE n.node_id=e.to_node_id)"
    ).fetchone()[0]
    assert dangling == 0


def test_apply_proposal_skips_unresolvable_edge(conn, tmp_path: Path) -> None:
    """An add_edge whose endpoints resolve to nothing real must be skipped rather
    than written as a dangling edge."""
    from forty_two_infer import apply_proposal

    gap = {"gap_type": "bucket", "parent_node_id": "scaffold-work-companies"}
    before = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    proposal = {
        "confidence": 0.9,
        "actions": [
            {
                "type": "add_edge",
                "from_node_id": "ghost-node-a",
                "to_node_id": "ghost-node-b",
                "rel_kind": "works_at",
            }
        ],
    }
    apply_proposal(conn, gap, proposal, data_dir=tmp_path)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    assert after == before  # nothing dangling was written


def test_queue_enqueue_and_drain(conn, tmp_path: Path) -> None:
    saved = _without_gemini_env()
    try:
        enqueue_graph_infer(conn, reason="test")
        assert has_graph_infer_pending(conn)
        r = drain_graph_infer_queue(conn, tmp_path, max_gaps=2)
        assert not has_graph_infer_pending(conn)
        assert "status" in r
    finally:
        _restore_env(saved)


def test_scheduler_drains_pending(conn, tmp_path: Path, fake_gemini) -> None:
    from forty_two_scheduler import tick

    fake_gemini(json.dumps({"confidence": 0.8, "actions": [], "unresolved_question": ""}))
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    enqueue_graph_infer(conn)
    conn.commit()
    tid = tick(conn, tmp_path)
    assert tid
