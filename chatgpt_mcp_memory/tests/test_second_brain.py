"""Tests for second-brain store CRUD and /today aggregate."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from store import (
    connect,
    seed_sync_sources,
    wiki_page_upsert,
    wiki_page_list,
    wiki_link_add,
    task_infer_insert,
    task_list,
    task_patch,
    output_create,
    system_issue_upsert,
    system_issues_open,
    ambient_event_insert_ignore,
)
from second_brain import build_today_bundle


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_wiki_and_task_crud(conn) -> None:
    pid = wiki_page_upsert(
        conn,
        page_id=None,
        page_type="person",
        title="Sarah Chen",
        body_md="VP Product",
    )
    pid2 = wiki_page_upsert(
        conn,
        page_id=None,
        page_type="project",
        title="Atlas",
        body_md="Launch project",
    )
    wiki_link_add(conn, from_page_id=pid, to_page_id=pid2)
    pages = wiki_page_list(conn, page_type="person")
    assert len(pages) == 1
    assert pages[0]["title"] == "Sarah Chen"

    tid = task_infer_insert(
        conn,
        title="Draft reply to Sarah",
        body_md="About launch timing",
        origin="agent",
    )
    rows = task_list(conn, origin="agent")
    assert len(rows) == 1
    patched = task_patch(conn, tid, status="review")
    assert patched is not None
    assert patched["status"] == "review"

    oid = output_create(conn, task_id=tid, kind="email_draft", body_md="Hi Sarah,")
    assert oid


def test_today_bundle_empty(conn, tmp_path: Path) -> None:
    bundle = build_today_bundle(conn, tmp_path)
    assert "working_context" in bundle
    assert "attention_24h" in bundle
    assert "work_items" in bundle
    assert "next_steps" in bundle


def test_system_issues(conn) -> None:
    system_issue_upsert(
        conn,
        issue_id="test-issue",
        severity="warning",
        source_key="ambient_loop",
        body_md="Something broke",
    )
    open_rows = system_issues_open(conn)
    assert any(r["issue_id"] == "test-issue" for r in open_rows)


def test_ambient_events_since(conn) -> None:
    now = time.time()
    ambient_event_insert_ignore(
        conn,
        event_type="window_focus",
        captured_at=now,
        dedupe_key=f"wf:{now}:TestApp\x1fTitle",
        payload={"app_name": "TestApp", "window_title": "Title"},
    )
    from store import ambient_events_since

    rows = ambient_events_since(conn, since_ts=now - 10)
    assert len(rows) >= 1


def test_today_bundle_next_steps_from_corpus(conn, tmp_path: Path, monkeypatch) -> None:
    def fake_prefetch(_conn, _data_dir, *, query_text, top_k=5, for_mcp=False):
        _ = (query_text, top_k, for_mcp)
        return [
            {
                "chunk_id": "c-kate",
                "score": 0.67,
                "path": "exports/kate.md",
                "kind": "chatgpt-export",
                "text": "Kate Larson V1 due tomorrow morning — LinkedIn CTA and practiceoflife.com fixes.",
            }
        ]

    monkeypatch.setattr("second_brain._prefetch_memory_hits", fake_prefetch)
    bundle = build_today_bundle(conn, tmp_path)
    kinds = {i["kind"] for i in bundle["next_steps"]["items"]}
    assert "corpus_signal" in kinds
    assert bundle["next_steps"]["signals"]["corpus_signals"] >= 1


def test_today_bundle_next_steps_from_tasks_and_screen(conn, tmp_path: Path) -> None:
    now = time.time()
    task_infer_insert(
        conn,
        title="Finish Kate Larson V1 fixes",
        body_md="Thursday morning review call.",
        origin="agent",
        priority="high",
    )
    ambient_event_insert_ignore(
        conn,
        event_type="window_snapshot",
        captured_at=now,
        dedupe_key=f"ws:{now}:deadline",
        payload={
            "app_name": "Slack",
            "window_title": "Practice of Life deadline",
            "ax_text_sample": "V1 implementation due tomorrow morning for Kate Larson.",
        },
    )
    conn.commit()

    bundle = build_today_bundle(conn, tmp_path)
    next_steps = bundle["next_steps"]
    assert next_steps["items"]
    assert next_steps["signals"]["deadline_signals"] >= 1
    assert any(i["kind"] == "task" for i in next_steps["items"])
    assert any(i["kind"] == "screen_signal" for i in next_steps["items"])
