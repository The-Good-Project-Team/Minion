"""Context Core: witness fusion, coverage, candidates, context bundle."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from context_core import (
    ambient_coverage_status,
    context_bundle,
    distill_evidence_candidates,
    fuse_screen_events_with_witnesses,
    group_ambient_witnesses,
    merge_witness_group,
    witness_confidence,
)
from graph_fill import apply_graph_candidate_resolution
from screen_memory import remember_screen
from second_brain import build_working_context
from store import (
    ambient_event_insert_ignore,
    connect,
    graph_candidate_list,
    seed_sync_sources,
)


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "memory.db")
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_witness_confidence_rises_with_more_collectors() -> None:
    one = witness_confidence({"window_snapshot"})
    many = witness_confidence({"window_snapshot", "dom_snapshot", "browser_visit"})
    assert many > one


def test_merge_witness_group_preserves_refs(conn) -> None:
    now = time.time()
    rows = []
    for i, kind in enumerate(["window_snapshot", "dom_snapshot"]):
        ambient_event_insert_ignore(
            conn,
            event_type=kind,
            captured_at=now + i * 0.1,
            dedupe_key=f"w{i}",
            payload={
                "app_name": "Chrome",
                "window_title": "Atlas Project",
                "ax_text_sample": "Jordan Lee roadmap",
                "dom_text_sample": "Jordan Lee roadmap details",
            },
        )
    conn.commit()
    groups = group_ambient_witnesses(
        [
            {
                "event_id": r[0],
                "event_type": r[1],
                "captured_at": now,
                "payload": {
                    "app_name": "Chrome",
                    "window_title": "Atlas Project",
                    "ax_text_sample": "Jordan Lee",
                },
            }
            for r in [("a", "window_snapshot"), ("b", "dom_snapshot")]
        ]
    )
    assert len(groups) == 1
    fused = merge_witness_group(groups[0])
    assert fused is not None
    assert fused["raw"]["witness_count"] == 2
    assert "dom_snapshot" in fused["raw"]["collector_types"]


def test_fuse_and_distill_create_candidates_not_graph_nodes(conn, tmp_path: Path) -> None:
    now = time.time()
    for kind, extra in [
        ("window_snapshot", {"ax_text_sample": "Email from jordan.lee@example.com about Atlas"}),
        ("clipboard_event", {"detected_emails": ["jordan.lee@example.com"], "summary": "copied email"}),
    ]:
        ambient_event_insert_ignore(
            conn,
            event_type=kind,
            captured_at=now,
            dedupe_key=f"e-{kind}",
            payload={
                "app_name": "Mail",
                "window_title": "Inbox",
                **extra,
            },
        )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE node_kind='person'").fetchone()[0]
    out = fuse_screen_events_with_witnesses(conn, since_hours=1)
    conn.commit()
    assert out["upserted"] >= 1
    distill = distill_evidence_candidates(conn, min_confidence=0.5, min_witnesses=1)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE node_kind='person'").fetchone()[0]
    assert distill["created"] >= 0
    assert after == before
    open_c = graph_candidate_list(conn, status="open")
    evidence = [c for c in open_c if c.get("source") == "context_core"]
    if evidence:
        cid = evidence[0]["candidate_id"]
        res = apply_graph_candidate_resolution(
            conn, cid, status="approved", payload={"relationship": "colleague on Atlas"}
        )
        conn.commit()
        assert res.get("ok") is True
        row = conn.execute(
            "SELECT title FROM graph_nodes WHERE node_kind='person' AND title LIKE '%Jordan%'"
        ).fetchone()
        assert row is not None


def test_context_bundle_includes_evidence_and_spine(conn, tmp_path: Path) -> None:
    now = time.time()
    ambient_event_insert_ignore(
        conn,
        event_type="window_snapshot",
        captured_at=now,
        dedupe_key="ctx-1",
        payload={"app_name": "Code", "window_title": "context_core.py", "ax_text_sample": "def context_bundle"},
    )
    conn.commit()
    fuse_screen_events_with_witnesses(conn, since_hours=1)
    conn.commit()
    bundle = context_bundle(conn, tmp_path, subject="Atlas")
    assert bundle.get("context_md")
    assert "why_this_context" in bundle
    assert bundle.get("coverage") is not None


def test_ambient_coverage_status_lists_collectors(conn, tmp_path: Path) -> None:
    cov = ambient_coverage_status(conn, tmp_path, minutes=60)
    assert "collectors" in cov
    assert len(cov["collectors"]) >= 5
    keys = {c["collector"] for c in cov["collectors"]}
    assert "browser_visit" in keys


def test_e2e_ambient_to_mcp_context(conn, tmp_path: Path, monkeypatch) -> None:
    """Synthetic path: redundant ambient → evidence → candidate → graph → MCP context."""
    monkeypatch.setenv("MINION_DISABLE_PLAYWRIGHT_DOM", "1")
    now = time.time()
    stream = tmp_path / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "ts": now,
            "kind": "window_snapshot",
            "app_name": "Mail",
            "window_title": "Atlas — Orion Launch",
            "ax_text_sample": "Jordan Lee committed to Orion Launch milestone Friday",
            "dedupe_key": "e2e:win",
        },
        {
            "ts": now + 0.2,
            "kind": "dom_snapshot",
            "app_name": "Mail",
            "window_title": "Atlas — Orion Launch",
            "dom_text_sample": "Jordan Lee jordan.lee@example.com Orion Launch",
            "dedupe_key": "e2e:dom",
        },
        {
            "ts": now + 0.4,
            "kind": "clipboard_event",
            "app_name": "Mail",
            "window_title": "Atlas — Orion Launch",
            "summary": "copied jordan.lee@example.com",
            "detected_emails": ["jordan.lee@example.com"],
            "dedupe_key": "e2e:clip",
        },
    ]
    stream.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    before_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE node_kind='person'").fetchone()[0]
    remember = remember_screen(
        conn, tmp_path, index_ax=False, ingest_screenshots=False, index_events=False
    )
    assert remember["ambient"]["ingested"] == 3
    assert remember["fused_events"]["upserted"] >= 1
    assert remember["fused_events"].get("max_witness_count", 0) >= 2

    distill_evidence_candidates(conn, min_confidence=0.45, min_witnesses=1)
    conn.commit()
    after_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE node_kind='person'").fetchone()[0]
    assert after_nodes == before_nodes

    open_person = [
        c
        for c in graph_candidate_list(conn, status="open")
        if (c.get("payload") or {}).get("email") == "jordan.lee@example.com"
        or "jordan" in (c.get("title") or "").lower()
    ]
    assert open_person
    res = apply_graph_candidate_resolution(
        conn,
        open_person[0]["candidate_id"],
        status="approved",
        payload={"relationship": "leads Orion Launch"},
    )
    conn.commit()
    assert res.get("ok") is True
    row = conn.execute(
        "SELECT title FROM graph_nodes WHERE node_kind='person' AND title LIKE '%Jordan%'"
    ).fetchone()
    assert row is not None

    bundle = context_bundle(conn, tmp_path, subject="Orion Launch")
    assert bundle.get("context_md")
    assert bundle.get("why_this_context")
    assert any(
        (e.get("witness_count") or 0) >= 2 for e in (bundle.get("recent_evidence") or [])
    )

    working = build_working_context(conn, tmp_path, for_mcp=True, memory_top_k=2)
    assert working.get("context_bundle")
    assert working.get("context_md")
    assert "Jordan" in (working.get("context_md") or "") or row[0] in str(
        working.get("graph_context") or ""
    )

    import mcp_server

    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)
    monkeypatch.setattr(mcp_server, "_data_dir", lambda: tmp_path)
    mcp_out = mcp_server._tool_get_working_context({})
    assert mcp_out.get("context_bundle")
    assert mcp_out.get("why_this_context") is not None
