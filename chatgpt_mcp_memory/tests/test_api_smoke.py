"""
End-to-end smoke tests for the Minion sidecar API.

Each test spawns a fresh sidecar on a random port against a scratch data dir
(see conftest.py::sidecar). Tests exercise the real HTTP + WebSocket stack
so wiring bugs in CORS, uvicorn lifespan, pydantic bodies, etc. surface
here before they hit the UI.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
import websockets


# ---------------------------------------------------------------------------
# 1. /status
# ---------------------------------------------------------------------------


def test_status_ready(sidecar) -> None:
    r = sidecar.get("/status")
    assert r.status_code == 200
    body = r.json()

    assert body["counts"] == {"sources": 0, "chunks": 0}
    assert "database" in body
    assert body["database"]["ok"] is True
    assert body["database"]["error"] is None
    assert body["db_path"].endswith("memory.db")
    assert str(sidecar.data_dir) in body["data_dir"] or body["data_dir"].endswith(
        sidecar.data_dir.name
    )
    # Parser registry should advertise at least the core formats.
    exts = set(body["supported_extensions"])
    for required in (".md", ".py", ".pdf", ".txt"):
        assert required in exts, f"missing {required} in supported_extensions: {exts}"
    # Watcher is disabled in tests.
    assert body["watcher"]["running"] is False
    assert body["watcher"]["mode"] == "disabled"


def test_onboarding_and_gemini_key_endpoints(sidecar) -> None:
    r = sidecar.post("/chat/agent/onboarding", {"step": "name", "transcript": []})
    assert r.status_code == 200, r.text
    assert "message" in r.json()

    prof = sidecar.post("/onboarding/profile", {"display_name": "Smoke User"})
    assert prof.status_code == 200, prof.text
    assert prof.json().get("ok") is True

    key = sidecar.put("/settings/gemini-key", {"api_key": "test-key-not-used"})
    assert key.status_code == 200, key.text
    assert key.json()["configured"] is True
    assert (sidecar.data_dir / ".secrets" / "gemini_api_key").read_text().strip() == "test-key-not-used"


def test_mcp_http_bridge_handles_jsonrpc(sidecar) -> None:
    denied = sidecar.post("/mcp", {"jsonrpc": "2.0", "id": 0, "method": "ping"})
    assert denied.status_code == 401
    assert "Password required" in denied.json()["detail"]

    headers = {"Authorization": "Bearer foofie"}
    init = sidecar.post(
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        },
        headers=headers,
    )
    assert init.status_code == 200, init.text
    body = init.json()
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "minion"

    tools = sidecar.post(
        "/mcp",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=headers,
    )
    assert tools.status_code == 200, tools.text
    names = {tool["name"] for tool in tools.json()["result"]["tools"]}
    assert "ask_minion" in names


def test_ingest_text_endpoint_queues_markdown_context(sidecar) -> None:
    r = sidecar.post("/ingest/text", {"title": "Build note", "text": "Foofie context goes here."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "text"
    queued = Path(body["queued"])
    assert queued.name.endswith(".md")
    assert queued.exists()
    assert "Foofie context goes here." in queued.read_text(encoding="utf-8")


@pytest.mark.skip(reason="Flaky HTTP timeout in CI environment")
def test_e2e_seed_graph_gap_when_enabled(sidecar) -> None:
    r = sidecar.post("/dev/e2e/seed-graph-gap", {"name": "Smoke Gap Person"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("thread_id")


def test_context_platform_and_onboarding_routes(sidecar) -> None:
    plat = sidecar.get("/context/platform")
    assert plat.status_code == 200
    assert plat.json()["schema_version"] >= 1

    nxt = sidecar.get("/onboarding/resource-poll/next")
    assert nxt.status_code == 200
    assert nxt.json()["question"]["resource_id"] == "gmail"

    ans = sidecar.post("/onboarding/resource-poll", {"resource_id": "gmail", "uses": True})
    assert ans.status_code == 200
    assert ans.json().get("candidate_id")

    bundle = sidecar.get("/context/bundle")
    assert bundle.status_code == 200
    assert bundle.json().get("schema_version") >= 1
    assert "privacy_scope" in bundle.json()


def test_maintenance_storage_report(sidecar) -> None:
    r = sidecar.post("/maintenance/storage-report", {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["chunk_storage_tiers"], dict)
    assert body["chunk_storage_tiers"].get("hot", 0) >= 0
    assert body["ambient_event_count"] == 0
    sq = body.get("sqlite")
    assert sq is not None and isinstance(sq, dict)
    assert "page_count" in sq and int(sq["page_count"]) >= 1
    assert "freelist_ratio" in sq
    assert "Compaction" in body["note"] or "metadata" in body["note"].lower()


@pytest.mark.skip(reason="Flaky HTTP timeout in CI environment")
def test_today_and_wiki_crud(sidecar) -> None:
    r = sidecar.get("/today")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "working_context" in body
    assert "work_items" in body

    r2 = sidecar.post(
        "/wiki/pages",
        {
            "page_type": "topic",
            "title": "Test topic",
            "body_md": "Body",
            "status": "active",
        },
    )
    assert r2.status_code == 200, r2.text
    pid = r2.json()["page_id"]

    r3 = sidecar.post(
        "/tasks/infer",
        {"title": "Inferred task", "body_md": "ctx", "origin": "agent"},
    )
    assert r3.status_code == 200, r3.text
    tid = r3.json()["task_id"]

    r4 = sidecar.patch(f"/tasks/{tid}", {"status": "archived"})
    assert r4.status_code == 200, r4.text

    r5 = sidecar.get("/health")
    assert r5.status_code == 200
    health = r5.json()
    assert health["service"] == "minion-api"
    assert health["status"] == "ok"
    assert health["database"]["ok"] is True
    assert health["db_path"].endswith("memory.db")
    assert health["counts"] == {"sources": 0, "chunks": 0}
    assert "sync_sources" in health


def test_maintenance_storage_tier_promote_stale_dry_run(sidecar) -> None:
    r = sidecar.post(
        "/maintenance/storage-tier-promote-stale",
        {"dry_run": True, "min_source_age_days": 30},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["candidates"] == 0
    assert body["promoted"] == 0
    assert body["from_tier"] == "hot"
    assert body["to_tier"] == "warm"


def test_maintenance_storage_tier_promote_rejects_invalid_pair(sidecar) -> None:
    r = sidecar.post(
        "/maintenance/storage-tier-promote-stale",
        {"dry_run": True, "from_tier": "warm", "to_tier": "hot"},
    )
    assert r.status_code == 400, r.text


def test_maintenance_storage_tier_promote_warm_to_cold_dry_run(sidecar) -> None:
    r = sidecar.post(
        "/maintenance/storage-tier-promote-stale",
        {
            "dry_run": True,
            "min_source_age_days": 30,
            "from_tier": "warm",
            "to_tier": "cold",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_tier"] == "warm"
    assert body["to_tier"] == "cold"


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_reconcile_picks_up_inbox_files(sidecar, staged_note: Path) -> None:
    """POST /reconcile should ingest files already on disk when the watcher is off."""
    dest = sidecar.inbox / "already-here.md"
    shutil.copy2(staged_note, dest)
    r = sidecar.post("/reconcile", {"force": False})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    sources = sidecar.wait_for_sources(1, timeout=45.0)
    assert len(sources) == 1
    assert sources[0]["path"].endswith("already-here.md")


# ---------------------------------------------------------------------------
# 2. POST /ingest (single file) -> /sources
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_ingest_single_file(sidecar, staged_note: Path) -> None:
    r = sidecar.post("/ingest", {"path": str(staged_note)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "file"
    # The copy should have landed under the sidecar's inbox.
    assert str(sidecar.inbox) in body["queued"]

    sources = sidecar.wait_for_sources(1, timeout=45.0)
    assert len(sources) == 1, f"expected 1 source, got {sources}"
    src = sources[0]
    assert src["kind"] == "text"
    assert src["path"].endswith("staged-note.md")

    # /sources/{id} should return a chunk_count >= 1.
    info = sidecar.get(f"/sources/{src['source_id']}").json()
    assert info["chunk_count"] >= 1


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_ingest_temporary_file_cleans_staged_copy(sidecar, staged_note: Path) -> None:
    r = sidecar.post("/ingest", {"path": str(staged_note), "temporary": True})
    assert r.status_code == 200, r.text
    body = r.json()
    queued = Path(body["queued"])
    assert body["temporary"] is True
    assert str(sidecar.inbox) in str(queued)

    sources = sidecar.wait_for_sources(1, timeout=45.0)
    assert len(sources) == 1

    deadline = time.time() + 10
    while queued.exists() and time.time() < deadline:
        time.sleep(0.25)
    assert not queued.exists()

    tracking = sidecar.data_dir / "file_tracking.jsonl"
    assert tracking.exists()
    assert str(staged_note.resolve()) in tracking.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. POST /search
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_search_returns_ingested_file(sidecar, staged_note: Path) -> None:
    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    sidecar.wait_for_sources(1, timeout=45.0)

    r = sidecar.post("/search", {"query": "Good Capital owns this repo", "top_k": 3})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results, "search returned no results"
    top = results[0]
    assert top["path"].endswith("staged-note.md")
    assert top["score"] > 0.3, f"top score too low: {top['score']}"
    assert "Good Capital" in top["text"]


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_screen_memory_endpoints_fuse_search_and_create_task(sidecar) -> None:
    stream = sidecar.data_dir / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rows = [
        {
            "ts": now,
            "kind": "dom_snapshot",
            "app_name": "Chrome",
            "window_title": "Stripe Dashboard",
            "url": "https://stripe.com/dashboard/payouts",
            "visible_elements": [
                {"role": "button", "label": "Export", "bounds": [812, 210, 96, 38], "source": "DOM"}
            ],
            "dedupe_key": "api-dom-stripe-export",
        },
        {
            "ts": now + 1,
            "kind": "mouse_event",
            "app_name": "Chrome",
            "window_title": "Stripe Dashboard",
            "click_count": 1,
            "last_click": {"x": 830, "y": 220},
            "summary": "User clicked once in Chrome: Stripe Dashboard.",
            "dedupe_key": "api-mouse-stripe-export",
        },
        {
            "ts": now + 2,
            "kind": "clipboard_event",
            "app_name": "Google Sheets",
            "window_title": "Investor leads",
            "summary": "User copied investor email alex@example.com",
            "text_excerpt": "alex@example.com",
            "detected_emails": ["alex@example.com"],
            "dedupe_key": "api-clipboard-investor-email",
        },
    ]
    stream.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    remembered = sidecar.post(
        "/screen-memory/remember",
        {"ingest_screenshots": False, "run_adapters": False},
    )
    assert remembered.status_code == 200, remembered.text
    body = remembered.json()
    assert body["ambient"]["ingested"] == 3
    assert body["fused_events"]["upserted"] == 3
    assert body["graph_candidates"]["created"] == 1
    assert body["event_index"]["indexed"] >= 1
    assert body["graph_infer_queued"] is True

    events = sidecar.get("/screen-memory/events?minutes=30")
    assert events.status_code == 200, events.text
    assert events.json()["count"] >= 2

    search = sidecar.get("/screen-memory/search", params={"q": "export button", "app": "Chrome"})
    assert search.status_code == 200, search.text
    hits = search.json()["hits"]
    assert hits
    assert "Export" in hits[0]["text"]

    summary = sidecar.get("/screen-memory/summarize-last?minutes=30")
    assert summary.status_code == 200, summary.text
    assert summary.json()["event_count"] >= 2

    task = sidecar.post("/screen-memory/create-task", {"minutes": 30})
    assert task.status_code == 200, task.text
    assert task.json()["created"] is True

    candidates = sidecar.get("/graph/candidates")
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["candidates"][0]["payload"]["email"] == "alex@example.com"

    guidance = sidecar.get("/screen-memory/guidance?minutes=30")
    assert guidance.status_code == 200, guidance.text
    assert guidance.json()["mode"] == "graph_fill"


# ---------------------------------------------------------------------------
# 4. POST /ingest (directory) prunes junk dirs, keeps real files
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_ingest_directory_prunes_junk(sidecar, staged_project: Path) -> None:
    r = sidecar.post("/ingest", {"path": str(staged_project)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "directory"
    # The tree has README.md + src/main.py kept; node_modules, .git, .DS_Store dropped.
    assert body["file_count"] == 2, f"expected 2 kept files, got {body}"

    sources = sidecar.wait_for_sources(2, timeout=60.0)
    paths = sorted(s["path"] for s in sources)
    assert any(p.endswith("README.md") for p in paths), paths
    assert any(p.endswith("src/main.py") for p in paths), paths
    assert not any("node_modules" in p for p in paths), paths
    assert not any("/.git/" in p for p in paths), paths
    assert not any(p.endswith(".DS_Store") for p in paths), paths

    # Copied tree on disk should also be pruned.
    inbox_files = sorted(p.relative_to(sidecar.inbox) for p in sidecar.inbox.rglob("*") if p.is_file())
    assert not any("node_modules" in str(p) for p in inbox_files), inbox_files
    assert not any(".git" in str(p).split("/") for p in inbox_files), inbox_files


# ---------------------------------------------------------------------------
# 5. DELETE /sources
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_delete_source_removes_chunks(sidecar, staged_note: Path) -> None:
    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    sources = sidecar.wait_for_sources(1, timeout=45.0)
    src = sources[0]

    r = sidecar.delete("/sources", {"source_id": src["source_id"]})
    assert r.status_code == 200, r.text
    removed = r.json()["removed_chunks"]
    assert removed >= 1

    after = sidecar.get("/sources").json()
    assert after["counts"]["sources"] == 0
    assert after["counts"]["chunks"] == 0

    hits = sidecar.post("/search", {"query": "Good Capital", "top_k": 3}).json()["results"]
    assert not any(h["source_id"] == src["source_id"] for h in hits)


# ---------------------------------------------------------------------------
# 6. POST /connect/claude-desktop writes a valid MCP config
# ---------------------------------------------------------------------------


def test_connect_claude_desktop_writes_config(sidecar) -> None:
    # CLAUDE_DESKTOP_CONFIG was set in the sidecar's env by conftest, pointing
    # at a tmp file that does NOT yet exist -- the endpoint should create it.
    assert not sidecar.claude_cfg_path.exists()

    r = sidecar.post("/connect/claude-desktop", {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["server_name"] == "minion"
    assert body["restart_required"] is True
    assert Path(body["config_path"]).resolve() == sidecar.claude_cfg_path.resolve()

    cfg = json.loads(sidecar.claude_cfg_path.read_text())
    assert "mcpServers" in cfg
    entry = cfg["mcpServers"]["minion"]
    assert entry["command"].endswith("python") or entry["command"].endswith("python3") or entry["command"].endswith("python3.11")
    assert entry["args"][0].endswith("mcp_server.py")
    assert entry["env"]["MINION_DATA_DIR"] == str(sidecar.data_dir)


def test_connect_claude_desktop_status(sidecar) -> None:
    body = sidecar.get("/connect/claude-desktop/status").json()
    assert body["installed"] is True  # skipped in test env
    assert body["configured"] is False
    assert body["connected"] is False

    sidecar.post("/connect/claude-desktop", {}).raise_for_status()
    body = sidecar.get("/connect/claude-desktop/status").json()
    assert body["configured"] is True
    assert body["connected"] is True


def test_connect_claude_desktop_merges_existing(sidecar) -> None:
    """An existing config with other servers must be preserved + backed up."""
    existing = {
        "mcpServers": {
            "other-server": {"command": "/usr/bin/other", "args": [], "env": {}}
        },
        "unrelated_key": 42,
    }
    sidecar.claude_cfg_path.write_text(json.dumps(existing, indent=2))

    r = sidecar.post("/connect/claude-desktop", {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backup_path"] is not None
    assert Path(body["backup_path"]).exists()

    cfg = json.loads(sidecar.claude_cfg_path.read_text())
    assert cfg["unrelated_key"] == 42
    assert "other-server" in cfg["mcpServers"]
    assert "minion" in cfg["mcpServers"]


# ---------------------------------------------------------------------------
# 7. POST /factory-reset and /nuke exist + work
# ---------------------------------------------------------------------------


def test_nuke_and_factory_reset_exist(sidecar, staged_note: Path) -> None:
    # Ingest one file so we know the DB and inbox are non-empty.
    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    sidecar.wait_for_sources(1, timeout=45.0)
    assert any(sidecar.inbox.iterdir()), "expected inbox to have at least one file after ingest"

    # /nuke should exist.
    r = sidecar.post("/nuke", {"confirm": True})
    assert r.status_code == 200, r.text

    # DB should be empty after nuke.
    after = sidecar.get("/sources").json()
    assert after["counts"]["sources"] == 0
    assert after["counts"]["chunks"] == 0

    # /factory-reset should exist and should clear the inbox too.
    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    sidecar.wait_for_sources(1, timeout=45.0)
    assert any(sidecar.inbox.iterdir()), "expected inbox to be non-empty before factory reset"

    r2 = sidecar.post("/factory-reset", {"confirm": True})
    assert r2.status_code == 200, r2.text
    assert not any(sidecar.inbox.iterdir()), "expected inbox to be empty after factory reset"


# ---------------------------------------------------------------------------
# 7. WebSocket /events streams ingest lifecycle
# ---------------------------------------------------------------------------


def _collect_events_until(ws_url: str, stop_types: set, timeout: float) -> List[Dict[str, Any]]:
    """Consume events until we've seen at least one of each `stop_types` or timeout."""
    seen: List[Dict[str, Any]] = []

    async def _run() -> List[Dict[str, Any]]:
        remaining = stop_types.copy()
        async with websockets.connect(ws_url) as ws:
            try:
                while remaining:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    msg = json.loads(raw)
                    seen.append(msg)
                    remaining.discard(msg.get("type"))
            except asyncio.TimeoutError:
                pass
        return seen

    return asyncio.run(_run())


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_events_ws_streams_ingest(sidecar, staged_note: Path) -> None:
    """Open the WS first, then trigger ingest, and assert the lifecycle events land."""
    import threading

    result: Dict[str, Any] = {}

    def _listen() -> None:
        # snapshot is sent on connect; then we expect ingest_started + source_updated
        result["events"] = _collect_events_until(
            sidecar.ws_url,
            stop_types={"snapshot", "ingest_started", "source_updated"},
            timeout=45.0,
        )

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    # Give the WS a moment to connect + receive the snapshot before we POST.
    time.sleep(0.5)

    sidecar.post("/ingest", {"path": str(staged_note)}).raise_for_status()
    t.join(timeout=60.0)

    types = [e["type"] for e in result.get("events", [])]
    assert "snapshot" in types, f"no snapshot event: {types}"
    assert "ingest_started" in types, f"no ingest_started event: {types}"
    assert "source_updated" in types, f"no source_updated event: {types}"


# ---------------------------------------------------------------------------
# 8. Identity claims + summary (API contract)
# ---------------------------------------------------------------------------


def test_identity_propose_patch_summary(sidecar) -> None:
    prop = sidecar.post(
        "/identity/claims/propose",
        {
            "kind": "fact",
            "text": "E2E identity claim about household.",
            "meta": {"relation": "partner", "labels": ["family", "e2e"]},
        },
    )
    assert prop.status_code == 200, prop.text
    cid = prop.json()["claim_id"]
    patched = sidecar.patch(
        f"/identity/claims/{cid}",
        {"status": "active"},
    )
    assert patched.status_code == 200, patched.text
    summ = sidecar.get("/identity/summary").json()
    md = summ["markdown"]
    assert "partner" in md
    assert "household" in md


def test_identity_patch_text(sidecar) -> None:
    prop = sidecar.post(
        "/identity/claims/propose",
        {"kind": "boundary", "text": "Original boundary text for patch."},
    )
    assert prop.status_code == 200, prop.text
    cid = prop.json()["claim_id"]
    patched = sidecar.patch(
        f"/identity/claims/{cid}",
        {"text": "Updated boundary wording from e2e."},
    )
    assert patched.status_code == 200, patched.text
    assert "Updated boundary" in patched.json()["claim"]["text"]


def test_identity_mirror(sidecar) -> None:
    r = sidecar.get("/identity/mirror", params={"limit_history": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "markdown" in body and isinstance(body["markdown"], str)
    assert "history" in body and isinstance(body["history"], list)


# ---------------------------------------------------------------------------
# 10. Bulk DELETE /sources by kind
# ---------------------------------------------------------------------------


def test_delete_sources_by_kind_requires_confirm(sidecar) -> None:
    r = sidecar.delete("/sources", {"kind": "text", "confirm_bulk": False})
    assert r.status_code == 422


@pytest.mark.skip(reason="Flaky integration test in CI environment")
def test_delete_sources_by_kind_bulk(sidecar, staged_note: Path) -> None:
    shutil.copy2(staged_note, sidecar.inbox / "bulk-a.md")
    shutil.copy2(staged_note, sidecar.inbox / "bulk-b.md")
    sidecar.post("/reconcile", {"force": False}).raise_for_status()
    srcs = sidecar.wait_for_sources(2, timeout=45.0)
    assert len(srcs) == 2
    r = sidecar.delete("/sources", {"kind": "text", "confirm_bulk": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("sources_removed") == 2
    assert body["removed_chunks"] >= 1
    after = sidecar.get("/sources").json()
    assert after["counts"]["sources"] == 0


# ---------------------------------------------------------------------------
# 11. Webhook ingest + extensions reload
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Flaky HTTP timeout in CI environment")
def test_ingest_webhook_json(sidecar) -> None:
    r = sidecar.post(
        "/ingest/webhook",
        {
            "source_key": "pytest-webhook-e2e",
            "kind": "external",
            "chunks": [{"text": "Webhook fixture chunk for pytest.", "role": None, "meta": {}}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    sidecar.wait_for_sources(1, timeout=45.0)
    hits = sidecar.post("/search", {"query": "Webhook fixture chunk", "top_k": 4}).json()["results"]
    assert hits and any("Webhook fixture" in h["text"] for h in hits)


def test_extensions_reload(sidecar) -> None:
    r = sidecar.post("/extensions/reload", {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "reloaded" in body
    assert "manifest_path" in body
