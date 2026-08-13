"""Tests for LLM graph corpus mining."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ingest import _embed, _get_model
import graph_corpus_mine as graph_mine_mod
from graph_corpus_mine import (
    graph_mine_status,
    maybe_run_query_graph_mine,
    pick_mining_targets,
    run_periodic_graph_mine_tick,
    run_graph_mine_tick,
    schedule_background_graph_mine,
)
from settings import load_settings, save_settings
from store import connect, meta_get, seed_sync_sources, upsert_source


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


def test_run_mine_tick_skips_without_gemini(conn, tmp_path: Path) -> None:
    saved = _without_gemini_env()
    try:
        out = run_graph_mine_tick(conn, tmp_path, max_llm_calls=2)
        assert out["status"] == "disabled"
    finally:
        _restore_env(saved)


def test_run_mine_tick_applies_me_profile(conn, tmp_path: Path, fake_gemini) -> None:
    os.environ["MINION_DETERMINISTIC_EMBEDDINGS"] = "1"
    text = (
        "About me biography background: Reif grew up in Michigan. "
        "My role and profession: I build Minion."
    )
    model = _get_model("deterministic")
    embeddings = _embed(model, [text])
    upsert_source(
        conn,
        path=str(tmp_path / "notes" / "me.md"),
        kind="text",
        sha256="me-profile",
        mtime=1.0,
        bytes_=len(text),
        parser="test",
        source_meta={},
        chunks=[(text, "user", {})],
        embeddings=embeddings,
    )
    conn.commit()
    response = {
        "confidence": 0.85,
        "actions": [
            {
                "type": "set_me_profile",
                "summary": "Grew up in Michigan; builds Minion.",
            }
        ],
        "unresolved_question": "",
    }
    server = fake_gemini(json.dumps(response))

    try:
        out = run_graph_mine_tick(conn, tmp_path, max_llm_calls=1)
    finally:
        os.environ.pop("MINION_DETERMINISTIC_EMBEDDINGS", None)

    assert out.get("filled", 0) >= 1, out
    assert server.requests
    row = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id='scaffold-me'"
    ).fetchone()
    meta = json.loads(row["summary"])
    assert "Michigan" in meta.get("user_profile", "")
    assert meta.get("stability") == "core"


def test_graph_mine_status_tracks_calls(conn, tmp_path: Path) -> None:
    sec = tmp_path / ".secrets"
    sec.mkdir()
    (sec / "gemini_api_key").write_text("fake", encoding="utf-8")
    st = graph_mine_status(conn, tmp_path)
    assert st["enabled"] is True
    assert st["max_calls_per_day"] >= 48
    assert st["periodic_interval_sec"] == 21600


def test_periodic_graph_mine_debounces(conn, tmp_path: Path) -> None:
    saved = _without_gemini_env()
    try:
        first = run_periodic_graph_mine_tick(conn, tmp_path, now=1000)
        second = run_periodic_graph_mine_tick(conn, tmp_path, now=1100)
    finally:
        _restore_env(saved)

    assert first["status"] == "disabled"
    assert second["status"] == "deferred"


def test_query_graph_mine_has_smart_limits(conn, tmp_path: Path) -> None:
    saved = _without_gemini_env()
    settings = load_settings(tmp_path)
    settings["graph_mine_on_query_enabled"] = True
    save_settings(tmp_path, settings)
    try:
        first = maybe_run_query_graph_mine(conn, tmp_path, "who is Tiffani", now=2000)
        second = maybe_run_query_graph_mine(conn, tmp_path, "who is Sean", now=2050)
        third = maybe_run_query_graph_mine(conn, tmp_path, "who is Tiffani", now=3000)
    finally:
        _restore_env(saved)

    assert first["status"] == "disabled"
    assert second["status"] == "deferred"
    assert third["status"] == "deferred"


def test_schedule_background_graph_mine_nonblocking(tmp_path: Path, monkeypatch) -> None:
    """Background worker must finish without blocking the caller (CI-safe: no concurrent DB init)."""
    monkeypatch.setenv("MINION_SQLITE_JOURNAL", "delete")
    db = tmp_path / "memory.db"
    boot = connect(db)
    seed_sync_sources(boot)
    boot.commit()
    boot.close()

    saved = _without_gemini_env()
    try:
        out = schedule_background_graph_mine(tmp_path)
        assert out["status"] == "scheduled"

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and graph_mine_mod._bg_running:
            time.sleep(0.05)
        assert not graph_mine_mod._bg_running, "background graph mine thread did not finish"

        verify = connect(db)
        try:
            assert meta_get(verify, "graph_mine_last_periodic")
        finally:
            verify.close()
    finally:
        _restore_env(saved)
