"""Session open: briefing + one request per visit."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from session_open import build_delta, open_session
from store import _new_id, ambient_event_insert_ignore, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def _sparse_person(conn, name: str = "Session Alex") -> str:
    nid = _new_id("gn")
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0.5, '[]', 'vault_local', ?, ?)",
        (nid, name, now, now),
    )
    conn.commit()
    return nid


def test_open_session_graph_gap_creates_request(conn, tmp_path: Path) -> None:
    _sparse_person(conn, "Session Alex")
    out = open_session(conn, tmp_path, display_name="Alex")
    assert out["ok"] is True
    assert out["briefing_md"]
    assert out["request_md"]
    assert out["request_kind"] in ("graph_gap", "graph_active")
    assert out.get("thread_id")


def test_second_open_briefing_mentions_ambient(conn, tmp_path: Path) -> None:
    _sparse_person(conn, "River")
    first = open_session(conn, tmp_path)
    assert first["ok"] is True
    first_at = float(first["opened_at"])

    ambient_event_insert_ignore(
        conn,
        event_type="window_focus",
        dedupe_key=f"e2e-figma-{first_at}",
        payload={"app_name": "Figma", "window_title": "Design"},
        captured_at=first_at + 1.0,
    )
    conn.commit()

    second = open_session(conn, tmp_path)
    assert second["ok"] is True
    delta = build_delta(conn, tmp_path, first_at)
    assert delta["ambient_event_count"] >= 1
    assert second["delta_summary"]["ambient_event_count"] >= 1
    assert "Figma" in second["briefing_md"] or "ambient" in second["briefing_md"].lower()


def test_session_open_http(sidecar) -> None:
    conn = connect(sidecar.data_dir / "memory.db")
    try:
        _sparse_person(conn, "HTTP Person")
        conn.commit()
    finally:
        conn.close()

    r = sidecar.post("/session/open", {"display_name": "HTTP User"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("briefing_md")
    assert body.get("request_md")

    bundle = sidecar.get("/context/bundle")
    assert bundle.status_code == 200
    assert bundle.json().get("session")
