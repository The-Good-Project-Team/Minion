from pathlib import Path

from connector_intent import (
    create_connector_intent,
    load_resource_poll,
    next_poll_question,
    record_poll_answer,
    record_freeform_connector_intent,
)
from store import connect, graph_candidate_list, seed_sync_sources, task_list


import pytest


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_resource_poll_flow(conn, tmp_path: Path) -> None:
    q = next_poll_question(tmp_path)
    assert q is not None
    assert q["resource_id"] == "gmail"

    out = record_poll_answer(conn, tmp_path, resource_id="gmail", answer=True)
    assert out["ok"] is True
    assert out.get("candidate_id")
    assert out.get("task_id")

    state = load_resource_poll(tmp_path)
    assert state["answers"]["gmail"]["uses"] is True

    open_c = [c for c in graph_candidate_list(conn, status="open") if c["candidate_type"] == "connector_intent"]
    assert len(open_c) >= 1
    tasks = task_list(conn, limit=20)
    assert any(t.get("origin") == "connector_intent" for t in tasks)


def test_freeform_connector_intent(conn, tmp_path: Path) -> None:
    out = record_freeform_connector_intent(conn, tmp_path, source_text="Please connect my Slack export")
    assert out["ok"] is True
    assert out.get("candidate_id")


def test_connector_dedupe(conn) -> None:
    a = create_connector_intent(conn, resource_id="chatgpt", source="test")
    b = create_connector_intent(conn, resource_id="chatgpt", source="test")
    assert a["candidate_id"] == b["candidate_id"]
    assert b.get("deduped") is True
