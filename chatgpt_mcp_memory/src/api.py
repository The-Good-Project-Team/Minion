"""
Minion local HTTP API.

Purpose: give the Tauri desktop app (or any trusted client) a small, typed
surface over the same SQLite store + ingest pipeline the MCP uses. The desktop
sidecar serves the trusted LAN by default. `MINION_API_TOKEN` protects non-MCP
LAN routes; `/mcp` requires the MCP password (see GET /capabilities).

Endpoints:
  GET  /status                      -> counts, inbox path, db path, watcher
  GET  /sources                     -> list sources (kind / path_glob / since / limit)
  GET  /sources/{source_id}         -> source metadata
  DELETE /sources                   -> body: {"path": "..."} OR {"source_id": "..."} OR bulk {"kind": "text", "confirm_bulk": true}
  POST /search                      -> body: {"query", "top_k", "kind"?, "path_glob"?, "role"?}
  GET  /search/stream               -> SSE: events `meta`, `hit` (JSON per line), `done`, optional `error`
  GET  /identity/claims             -> list identity claims (optional ?status=&kind=)
  POST /identity/claims/propose     -> same shape as MCP propose_identity_update
  PATCH /identity/claims/{claim_id} -> {"status"? , "text"? , "meta"? , "superseded_by"? }
  GET  /identity/claims/{claim_id}/edges
  GET  /identity/summary            -> { "markdown": "..." }
  GET  /identity/clusters
  POST /identity/clusters/rebuild   -> run embedding clustering job
  GET  /graph/context               -> graph-first compact context for local/LLM clients
  GET  /graph/candidates            -> unresolved graph merge/fact candidates
  POST /graph/candidates/{id}/resolve -> approve/reject/dismiss/merge a candidate
  GET  /graphify/status             -> Graphify CLI + shadow graph paths
  POST /graphify/shadow-build       -> bundle, extract, import graph_candidates only
  POST /graphify/reconcile          -> consistent scan + shadow/durable graph diff
  GET  /context/bundle              -> unified context (graph, evidence, candidates, retrieval)
  GET  /menu/status                  -> menu-bar badge/status payload
  POST /screen-memory/remember       -> ingest screen stream, AX text, screenshot OCR fallbacks
  GET  /screen-memory/search         -> semantic search over screen-derived memory
  GET  /screen-memory/summarize-last -> compact recent screen summary
  GET  /screen-memory/guidance       -> graph-first "do this" guidance
  GET  /screen-memory/events         -> fused semantic screen event records
  POST /screen-memory/create-task    -> create one inferred task from recent screen work
  POST /identity/export             -> write zip under data_dir/exports/
  GET  /chunks/{chunk_id}           -> one chunk for evidence drill-down
  GET  /capabilities                -> stable feature flags for local agent integrations
  POST /mcp                         -> MCP JSON-RPC over HTTP (password required)
  GET  /diagnostics/about           -> product blurb + privacy note (no secrets)
  GET  /diagnostics/log             -> JSON: redacted tail of ``MINION_LOG_FILE`` sidecar log
  GET  /diagnostics/log/text        -> plain text tail (paste into tickets)
  GET  /diagnostics/log/stream      -> SSE: live redacted log lines (loopback use)
  GET  /diagnostics/peers           -> JSON: Minion sidecars on 127.0.0.1 (scan ``MINION_PEER_SCAN_PORT_LO/HI``)
  POST /ingest                      -> body: {"path": "..."}  (copies path into inbox if outside)
  POST /ingest/webhook              -> JSON or NDJSON chunks (Bearer when MINION_API_TOKEN set)
  GET  /extensions                  -> parser_extensions.json schema + webhook docs
  POST /extensions/reload           -> re-read parser_extensions.json
  POST /reconcile                   -> body: {"force": bool}  rescan inbox → DB (optional re-embed all)
  WS   /events                      -> push ingest + heartbeat (see handler for `type` values)

Optional env:
  MINION_API_HOST — host for sidecar (default 0.0.0.0 for trusted LAN).
  MINION_MCP_HTTP_TOKEN — password/token for POST /mcp (default: foofie).
  MINION_ANALYTICS_URL — HTTPS URL for anonymous analytics (overrides bundled default).
  MINION_DISABLE_REMOTE_ANALYTICS=1 — do not set a collector URL (fork / air-gapped builds).

Run:
  python src/api.py --host 127.0.0.1 --port 8765
  # or
  uvicorn api:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from fastembed_cache import fastembed_cache_dir, register_fastembed_data_dir
from ingest import ingest_file, ingest_webhook_payload, _looks_like_chatgpt_export, _looks_like_claude_export, _looks_like_gemini_export, _looks_like_copilot_export
from parsers.chatgpt_export import validate_export_structure, ExportValidationError
from parser_extensions import manifest_path
from parsers import ALL_KINDS, load_user_extensions, supported_extensions, user_extension_mappings
from settings import apply_settings, load_settings, save_settings
import analytics_remote
import diagnostics
import telemetry
import consent_policy
import identity
from ambient_pipeline import ingest_screen_context_jsonl
from ambient_index import index_ax_from_stream
from ambient_scheduler import start_ambient_scheduler
from librarian_scheduler import start_librarian_scheduler
from second_brain import build_today_bundle, build_working_context
from graph_context import build_graph_context, build_menu_status
from graph_fill import apply_graph_candidate_resolution
from screen_memory import (
    create_task_from_recent_screen,
    miyagi_guidance,
    remember_screen,
    screen_search,
    screen_memory_status,
    summarize_last,
    what_was_i_doing,
)
from activity_feed import build_activity_feed
from council_api import handle_council_approve
import graph_clarify
import keys_api
from version import __version__
from export_bundle import write_identity_export_zip
from preference_cluster import run_preference_clustering
from retrieval_bias import rrf_fuse
from store import (
    DB_FILENAME,
    connect,
    count_chunks,
    count_sources,
    delete_source,
    delete_source_by_path,
    delete_sources_by_kind,
    fts_available,
    get_chunk,
    get_meta,
    get_source,
    identity_claim_get,
    identity_claim_list,
    identity_claim_mirror_history,
    identity_claim_patch_fields,
    identity_audit_log_append,
    identity_edges_for_claim,
    _row_identity_claim,
    graph_audit_log_append,
    audit_log_list,
    graph_scaffold_list,
    graph_candidate_get,
    graph_candidate_list,
    graph_candidate_resolve,
    ambient_events_recent,
    screen_memory_events_since,
    chunk_storage_tier_counts,
    sqlite_storage_fingerprint,
    count_chunks_stale_source_tier_promotion_candidates,
    promote_chunks_for_stale_sources,
    consolidate_chunks_to_warm,
    offload_chunks_to_cold,
    deduplicate_chunks_by_fingerprint,
    vacuum_database,
    validate_stale_tier_promotion,
    keyword_search as store_keyword_search,
    list_sources,
    preference_clusters_list,
    search as store_search,
    wiki_page_list,
    wiki_page_get,
    wiki_page_upsert,
    wiki_page_delete,
    wiki_link_add,
    wiki_links_for_page,
    task_list,
    task_get,
    task_infer_insert,
    task_patch,
    output_create,
    output_get,
    output_patch,
    sync_sources_list,
    system_issues_open,
    system_issue_resolve,
    system_issue_upsert,
)
import numpy as np


log = logging.getLogger("minion.api")


# ---------------------------------------------------------------------------
# Shared state (one connection per thread; the asyncio loop gets its own)
# ---------------------------------------------------------------------------


class State:
    data_dir: Path
    inbox: Path
    db_path: Path
    loop: Optional[asyncio.AbstractEventLoop] = None
    # sqlite3 connections are single-thread; FastAPI dispatches sync handlers
    # onto a threadpool, so we stash one connection per thread.
    _tls: threading.local = threading.local()
    subscribers: Set[WebSocket] = set()
    subscribers_lock: asyncio.Lock = None  # initialised in lifespan
    # Active-ingest snapshot (for UI progress card) + lock guarding it.
    active: Dict[str, Any] = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
    active_lock: threading.Lock = threading.Lock()
    # Set when connect/query fails; cleared on successful /status probe.
    db_error: Optional[str] = None
    # Cancel flags for active ingest operations (path -> cancel_flag dict)
    cancel_flags: Dict[str, Dict[str, bool]] = {}
    cancel_flags_lock: threading.Lock = threading.Lock()

    @classmethod
    def conn(cls) -> sqlite3.Connection:
        c = getattr(cls._tls, "conn", None)
        if c is None:
            c = connect(cls.db_path)
            cls._tls.conn = c
        return c


# Application Support folder name for this build's private data layer.
# Minion 2 (768-dim) must NOT share a data dir with Minion 1 (LEGACY, 384-dim):
# the embedders have incompatible vector widths. The Rust shell normally sets
# MINION_DATA_DIR (see desktop/src-tauri/src/lib.rs DATA_DIR_NAME); these keep the
# sidecar consistent when run standalone, and name the legacy dir we import from.
DATA_DIR_NAME = "Minion 2"
LEGACY_DATA_DIR_NAME = "Minion"


def _resolve_paths() -> None:
    env = os.environ.get("MINION_DATA_DIR")
    if env:
        State.data_dir = Path(env).expanduser().resolve()
    else:
        # Default to a user-level data directory.
        # The desktop shell always sets MINION_DATA_DIR, but this keeps the
        # sidecar consistent when run standalone.
        if sys.platform == "darwin":
            State.data_dir = Path.home() / "Library" / "Application Support" / DATA_DIR_NAME / "data"
        elif sys.platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            State.data_dir = Path(appdata) / DATA_DIR_NAME / "data" if appdata else Path.home() / ".minion" / "data"
        else:
            State.data_dir = Path.home() / ".minion" / "data"
    State.data_dir.mkdir(parents=True, exist_ok=True)
    register_fastembed_data_dir(State.data_dir)

    inbox_env = os.environ.get("MINION_INBOX")
    State.inbox = (
        Path(inbox_env).expanduser().resolve()
        if inbox_env
        else State.data_dir.parent / "inbox"
    )
    State.inbox.mkdir(parents=True, exist_ok=True)
    State.db_path = State.data_dir / DB_FILENAME


# ---------------------------------------------------------------------------
# WebSocket fanout — any ingest (from the watcher or the API) emits an event.
# ---------------------------------------------------------------------------


async def _broadcast(event: Dict[str, Any]) -> None:
    dead: List[WebSocket] = []
    async with State.subscribers_lock:
        targets = list(State.subscribers)
    for ws in targets:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    if dead:
        async with State.subscribers_lock:
            for ws in dead:
                State.subscribers.discard(ws)


def _schedule_broadcast(event: Dict[str, Any]) -> None:
    """Thread-safe entry point for background threads."""
    loop = State.loop
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(_broadcast(event), loop)


def _public_active() -> Dict[str, Any]:
    """Snapshot for /status and WS clients.

    When no batch is in flight (total <= 0), stale file_done events can leave
    done > 0 without a matching total; never expose that pair to the UI.
    """
    idle = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
    with State.active_lock:
        active = dict(State.active)
        if int(active.get("total") or 0) <= 0:
            if any(int(active.get(k) or 0) > 0 for k in ("done", "added", "skipped")):
                State.active = dict(idle)
            return dict(idle)
    return active


def _watcher_event_bridge(kind: str, payload: Dict[str, Any]) -> None:
    """Translate watcher/reconcile events into the WebSocket schema the UI expects."""
    if kind == "batch_started":
        with State.active_lock:
            State.active = {
                "root": "watcher",
                "total": int(payload.get("total", 0)),
                "done": 0,
                "added": 0,
                "skipped": 0,
            }
        _schedule_broadcast({
            "type": "ingest_started",
            "source": "watcher",
            "count": payload.get("total", 0),
            "active": dict(State.active),
        })
    elif kind == "file_started":
        _schedule_broadcast({
            "type": "ingest_progress",
            "path": payload.get("path"),
            "index": payload.get("index"),
            "total": payload.get("total"),
        })
    elif kind == "file_progress":
        _schedule_broadcast({
            "type": "file_progress",
            **{k: v for k, v in payload.items() if k != "type"},
        })
    elif kind == "file_done":
        skipped = bool(payload.get("skipped"))
        with State.active_lock:
            if int(State.active.get("total") or 0) <= 0:
                snap = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
            else:
                State.active["done"] = int(payload.get("index", State.active["done"]))
                if skipped:
                    State.active["skipped"] += 1
                elif payload.get("source_id"):
                    State.active["added"] += 1
                snap = dict(State.active)
        if not skipped:
            try:
                from librarian_queue import enqueue_graph_infer

                enqueue_graph_infer(State.conn(), reason="source_updated")
                State.conn().commit()
            except Exception:
                log.debug("graph infer enqueue skipped", exc_info=True)
        _schedule_broadcast({
            "type": "ingest_skipped" if skipped else "source_updated",
            "result": payload,
            "counts": _counts(),
            "active": snap,
        })
    elif kind == "file_failed":
        with State.active_lock:
            if int(State.active.get("total") or 0) <= 0:
                snap = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
            else:
                State.active["done"] = int(payload.get("index", State.active["done"]))
                State.active["skipped"] += 1
                snap = dict(State.active)
        _schedule_broadcast({
            "type": "ingest_failed",
            "path": payload.get("path"),
            "active": snap,
        })
    elif kind == "batch_done":
        snap = None
        with State.active_lock:
            snap = dict(State.active)
            State.active = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
        if int(snap.get("added", 0)) > 0:
            try:
                from librarian_queue import enqueue_graph_infer

                enqueue_graph_infer(State.conn(), reason="batch_done")
                State.conn().commit()
            except Exception:
                log.debug("graph infer enqueue on batch_done skipped", exc_info=True)
        _schedule_broadcast({
            "type": "tree_done",
            "root": "watcher",
            "added": snap.get("added", 0),
            "skipped": snap.get("skipped", 0),
            "counts": _counts(),
        })
    elif kind == "removed":
        _schedule_broadcast({
            "type": "source_removed",
            "key": payload.get("path"),
            "counts": _counts(),
        })
    elif kind == "error":
        msg = str(payload.get("message") or "watcher error")
        if len(msg) > 800:
            msg = msg[:800] + "…"
        State.db_error = msg
        _schedule_broadcast({"type": "db_error", "message": msg})


# ---------------------------------------------------------------------------
# Watcher integration — start the same watcher the MCP uses, but wire its
# per-file events into our websocket fanout so the UI updates live.
# ---------------------------------------------------------------------------


_watcher_thread: Optional[threading.Thread] = None
_heartbeat_thread: Optional[threading.Thread] = None
_watcher_mode: str = "disabled"
_manual_reconcile_lock = threading.Lock()


def _start_watcher(skip_reingest: bool = False) -> None:
    global _watcher_thread, _heartbeat_thread, _watcher_mode
    _watcher_mode = "disabled"
    if os.environ.get("MINION_DISABLE_WATCHER") in ("1", "true", "TRUE"):
        return
    # A just-rotated DB pauses the startup re-index (see _surface_db_rotate_flag)
    # unless explicitly overridden. The live watcher still starts, so anything
    # newly dropped into the inbox is still picked up.
    if skip_reingest and os.environ.get("MINION_DISABLE_REINGEST_GUARD") not in ("1", "true", "TRUE"):
        log.warning(
            "startup inbox re-index paused: database was just rotated aside; "
            "live watching active. Restart again or trigger a manual reconcile to re-index."
        )
    try:
        from watcher import reconcile_once, start_background, start_polling_watcher

        def _factory() -> sqlite3.Connection:
            return connect(State.db_path)

        # Reconcile in a background thread so lifespan startup finishes
        # immediately -- a large pre-existing inbox shouldn't block the
        # socket from binding. We broadcast "ready" once it's done.
        def _reconcile_bg() -> None:
            try:
                if skip_reingest and os.environ.get("MINION_DISABLE_REINGEST_GUARD") not in ("1", "true", "TRUE"):
                    # Don't replay the inbox onto a freshly-emptied DB.
                    State.db_error = None
                    _schedule_broadcast({"type": "ready", "counts": _counts()})
                    return
                bg_conn = connect(State.db_path)
                try:
                    reconcile_once(bg_conn, State.inbox, on_event=_watcher_event_bridge)
                finally:
                    bg_conn.close()
                State.db_error = None
                _schedule_broadcast({"type": "ready", "counts": _counts()})
            except Exception as e:
                log.exception("startup reconcile failed")
                msg = str(e)
                if len(msg) > 800:
                    msg = msg[:800] + "…"
                State.db_error = msg
                _schedule_broadcast({"type": "db_error", "message": msg})

        threading.Thread(
            target=_reconcile_bg, name="minion-api-reconcile", daemon=True
        ).start()

        _watcher_thread = start_background(
            _factory, State.inbox, on_event=_watcher_event_bridge
        )
        if _watcher_thread is not None:
            _watcher_mode = "watchdog"
        else:
            _watcher_thread = start_polling_watcher(
                _factory, State.inbox, on_event=_watcher_event_bridge
            )
            _watcher_mode = "polling"

        # Even with watchdog, we emit periodic heartbeats so the UI can show
        # a live count without polling the HTTP API.
        def _heartbeat() -> None:
            while True:
                time.sleep(5.0)
                try:
                    _schedule_broadcast({"type": "heartbeat", "counts": _counts()})
                except Exception:
                    pass

        _heartbeat_thread = threading.Thread(
            target=_heartbeat, name="minion-api-heartbeat", daemon=True
        )
        _heartbeat_thread.start()
    except Exception:
        log.exception("failed to start watcher")
        _watcher_mode = "disabled"


def _counts(profile_id: Optional[str] = None) -> Dict[str, Any]:
    try:
        conn = State.conn()
        if profile_id is None:
            from store import profile_get_active

            profile_id = profile_get_active(conn) or "default"
        return {
            "sources": count_sources(conn, profile_id=profile_id),
            "chunks": count_chunks(conn, profile_id=profile_id),
        }
    except Exception:
        return {"sources": 0, "chunks": 0}


def _database_status() -> Dict[str, Any]:
    """Cheap DB health for GET /status (per-request thread may open first connection)."""
    try:
        conn = State.conn()
        conn.execute("SELECT 1").fetchone()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        mode = str(row[0]) if row else None
        State.db_error = None
        return {"ok": True, "error": None, "journal_mode": mode}
    except Exception as e:
        msg = str(e)
        if len(msg) > 500:
            msg = msg[:500] + "…"
        State.db_error = msg
        return {"ok": False, "error": msg, "journal_mode": None}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


def _start_legacy_import() -> None:
    """Spawn the one-shot Minion 1 -> Minion 2 vault import (idempotent)."""
    if os.environ.get("MINION_DISABLE_LEGACY_IMPORT", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return

    def _run() -> None:
        try:
            import migrate_from_legacy

            result = migrate_from_legacy.run(
                State.data_dir, on_event=_watcher_event_bridge
            )
            log.info("legacy import: %s", result)
        except Exception:
            log.exception("legacy import thread crashed")

    threading.Thread(target=_run, name="minion-legacy-import", daemon=True).start()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    State.loop = asyncio.get_running_loop()
    State.subscribers_lock = asyncio.Lock()
    _resolve_paths()
    # First launch after upgrading from Minion 1: import the old vault from the
    # legacy "Minion" data dir. Runs in a background thread — Phase A (text copy)
    # makes keyword search work within seconds; Phase B re-embeds in the
    # background. No-ops once done, or if there's nothing to import.
    _start_legacy_import()
    telemetry.configure(State.data_dir)
    # Load & apply user preferences before the watcher starts scanning the
    # inbox — otherwise a reconcile pass could ingest kinds the user has
    # already turned off.
    try:
        apply_settings(load_settings(State.data_dir))
    except Exception:
        log.exception("failed to load settings")
    try:
        n_ext = load_user_extensions(State.data_dir)
        if n_ext:
            log.info("parser_extensions: loaded %s user mapping(s)", n_ext)
    except Exception:
        log.exception("failed to load parser_extensions.json")
    # Detect a just-rotated DB *before* the watcher reconcile runs, so we can
    # pause the auto-reindex (a freshly-emptied DB + a large inbox is what
    # produced the re-ingest CPU loop). Surfaces a health issue either way.
    just_rotated = False
    try:
        just_rotated = _surface_db_rotate_flag(State.data_dir, State.conn())
        State.conn().commit()
    except Exception:
        log.exception("db_rotate flag handling failed")
    _start_watcher(skip_reingest=just_rotated)
    # Initialize AI assistant connectors (Claude Desktop, Cursor, etc.)
    try:
        from connector_base import initialize_connectors

        initialize_connectors()
    except Exception:
        log.exception("connector initialization failed")
    # Nudge Claude Desktop to re-read our tool descriptions + retrieval policy
    # whenever the MCP-relevant sources have changed since last launch. No-op
    # if Claude's config file doesn't exist (user hasn't opted in yet).
    _refresh_mcp_on_launch()
    if not os.environ.get("MINION_DISABLE_AMBIENT_SCHEDULER", "").strip():
        start_ambient_scheduler(State.data_dir, State.conn)
    try:
        from file_tracker import start_file_tracker

        start_file_tracker(State.data_dir)
    except Exception:
        log.exception("file tracker failed to start")
    start_librarian_scheduler(State.data_dir, State.conn)
    try:
        from export_scheduler import start_export_scheduler

        start_export_scheduler(State.data_dir, State.conn)
    except Exception:
        log.exception("export scheduler failed to start")
    try:
        from graph_corpus_mine import schedule_background_graph_mine

        schedule_background_graph_mine(State.data_dir)
    except Exception:
        log.debug("boot graph mine schedule skipped", exc_info=True)
    try:
        analytics_remote.emit_session_if_ready()
    except Exception:
        pass
    try:
        # Opt-in: forward ERROR+ sidecar logs to the collector for monitoring.
        analytics_remote.install_log_monitor()
    except Exception:
        pass
    yield


app = FastAPI(title="Minion Local API", version=__version__, lifespan=_lifespan)
# Allow Vite dev server (different port) to hit the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:1420", "http://127.0.0.1:1420", "tauri://localhost"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _mutation_bearer_auth(request: Request, call_next):
    """Optional MINION_API_TOKEN: protect LAN API routes; /mcp handles its own password."""
    tok = os.environ.get("MINION_API_TOKEN", "").strip()
    path = request.url.path
    if request.method == "POST" and path == "/mcp":
        return await call_next(request)
    if not _is_loopback_client(request):
        auth = (request.headers.get("authorization") or "").strip()
        if not tok or auth != f"Bearer {tok}":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    if not tok or request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)
    if request.method == "POST" and path in ("/search",):
        return await call_next(request)
    auth = (request.headers.get("authorization") or "").strip()
    if auth != f"Bearer {tok}":
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


def _is_loopback_client(request: Request) -> bool:
    host = (request.client.host if request.client else "").strip().lower()
    return host in {"127.0.0.1", "::1", "localhost"}


def _mcp_http_token() -> str:
    return os.environ.get("MINION_MCP_HTTP_TOKEN", "").strip() or "foofie"


def _mcp_http_authorized(request: Request) -> bool:
    token = _mcp_http_token()
    auth = (request.headers.get("authorization") or "").strip()
    password = (request.headers.get("x-minion-password") or "").strip()
    return auth == f"Bearer {token}" or password == token


def _mcp_jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@app.post("/mcp")
async def mcp_http_endpoint(request: Request) -> JSONResponse:
    """MCP JSON-RPC over HTTP for trusted LAN clients.

    The stdio MCP remains the default for same-machine clients. This bridge is
    intentionally token-gated when reached off-loopback so a LAN bind does not
    silently expose the user's context.
    """
    if not _mcp_http_authorized(request):
        detail = "Password required for Minion MCP. Use password: foofie."
        return JSONResponse(
            {"detail": detail, "auth": {"type": "password", "password_hint": "foofie"}},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="Minion MCP", charset="UTF-8"'},
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(_mcp_jsonrpc_error(None, -32700, "Parse error"), status_code=400)

    try:
        from mcp_server import handle_jsonrpc
    except Exception as exc:
        return JSONResponse(_mcp_jsonrpc_error(None, -32603, f"MCP unavailable: {exc}"), status_code=500)

    def one(req: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(req, dict):
            return _mcp_jsonrpc_error(None, -32600, "Invalid Request")
        return handle_jsonrpc(req)

    if isinstance(payload, list):
        if not payload:
            return JSONResponse(_mcp_jsonrpc_error(None, -32600, "Invalid Request"), status_code=400)
        responses = [resp for resp in (one(req) for req in payload) if resp is not None]
        if not responses:
            return JSONResponse({}, status_code=202)
        return JSONResponse(responses)

    resp = one(payload)
    if resp is None:
        return JSONResponse({}, status_code=202)
    return JSONResponse(resp)


class SearchBody(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1, le=20)
    kind: Optional[str] = None
    path_glob: Optional[str] = None
    role: Optional[str] = None
    since: Optional[float] = None
    max_chars: int = Field(default=600, ge=50, le=4000)
    profile_id: Optional[str] = None


class IngestBody(BaseModel):
    path: str
    move: bool = False  # if True, move into inbox; else copy
    recursive: bool = True  # used when `path` is a directory
    temporary: bool = False  # remove staged inbox copy after indexing; original remains tracked
    refresh: bool = False  # for ChatGPT exports: only add new conversations (skip duplicates)
    profile_id: Optional[str] = None  # active profile when omitted


class ValidateExportBody(BaseModel):
    path: str


class CancelIngestBody(BaseModel):
    path: str


class IngestTextBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=500_000)
    title: str = Field(default="Quick context", max_length=120)


class WebhookChunk(BaseModel):
    text: str = Field(..., min_length=1)
    role: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class IngestWebhookBody(BaseModel):
    source_key: str = Field(..., min_length=1, max_length=200)
    display_name: Optional[str] = Field(None, max_length=500)
    kind: str = Field(default="external", max_length=64)
    parser: str = Field(default="webhook", max_length=64)
    chunks: List[WebhookChunk] = Field(..., min_length=1)

    @field_validator("chunks")
    @classmethod
    def cap_chunks(cls, v: List[WebhookChunk]) -> List[WebhookChunk]:
        if len(v) > 2000:
            raise ValueError("at most 2000 chunks per request")
        return v


SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env",
    "node_modules", "target", "build", "dist",
    "__pycache__", ".svelte-kit", ".next", ".nuxt",
    ".cache", ".DS_Store",
}


def _iter_files_in_tree(root: Path) -> List[Path]:
    """Walk a directory, skipping common build/cache dirs and dotfiles."""
    out: List[Path] = []
    stack: List[Path] = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                if p.name in SKIP_DIR_NAMES:
                    continue
                stack.append(p)
            elif p.is_file():
                out.append(p)
    return out


class DeleteBody(BaseModel):
    path: Optional[str] = None
    source_id: Optional[str] = None
    """Bulk: remove every source with this ``kind`` (e.g. ``text``). Requires ``confirm_bulk``."""

    kind: Optional[str] = None
    confirm_bulk: bool = False

    @model_validator(mode="after")
    def _delete_exactly_one_target(self) -> "DeleteBody":
        modes = sum(
            1
            for x in (self.path, self.source_id, self.kind)
            if x is not None and str(x).strip() != ""
        )
        if modes != 1:
            raise ValueError(
                "provide exactly one of: path, source_id, kind (bulk forget-all of that kind)"
            )
        if self.kind is not None and not self.confirm_bulk:
            raise ValueError("bulk delete by kind requires confirm_bulk: true")
        return self


class ConnectBody(BaseModel):
    server_name: str = "minion"
    config_path: Optional[str] = None


class SettingsBody(BaseModel):
    disabled_kinds: Optional[List[str]] = None
    telemetry_opt_out: Optional[bool] = None
    remote_monitoring: Optional[bool] = None
    ambient_sensing_enabled: Optional[bool] = None
    full_listening_enabled: Optional[bool] = None
    capture_on_empty_ax: Optional[bool] = None
    ambient_deny: Optional[Dict[str, Any]] = None
    ambient_collectors: Optional[Dict[str, bool]] = None


class ReconcileBody(BaseModel):
    force: bool = False


class IdentityProposeBody(BaseModel):
    kind: str
    text: str
    source_agent: Optional[str] = None
    confidence: Optional[float] = None
    evidence_chunk_ids: Optional[List[str]] = None
    evidence_rationales: Optional[List[Optional[str]]] = None
    meta: Optional[Dict[str, Any]] = None


class IdentityPatchBody(BaseModel):
    status: Optional[str] = None
    superseded_by: Optional[str] = None
    text: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    revision_source: Optional[str] = None

    @model_validator(mode="after")
    def _patch_one_field(self) -> "IdentityPatchBody":
        if (
            self.status is None
            and self.superseded_by is None
            and self.text is None
            and self.meta is None
            and self.revision_source is None
        ):
            raise ValueError(
                "provide at least one of: status, superseded_by, text, meta, revision_source"
            )
        return self


class ClusterRebuildBody(BaseModel):
    sample_limit: int = Field(default=1500, ge=100, le=5000)
    k: int = Field(default=8, ge=2, le=32)
    use_llm: bool = True


class IdentityExportBody(BaseModel):
    """Optional path; default writes to `<data_dir>/exports/minion-identity-<ts>.zip`."""

    out_path: Optional[str] = None
    include_chunk_index: bool = True
    include_voice_files: bool = True


class StorageTierPromoteStaleBody(BaseModel):
    """Advance chunk ``storage_tier`` (e.g. hot→warm→cold) when parent source is stale."""

    min_source_age_days: float = Field(default=120.0, ge=1.0, le=3650.0)
    source_kinds: Optional[List[str]] = None
    dry_run: bool = True
    from_tier: str = Field(default="hot", max_length=16)
    to_tier: str = Field(default="warm", max_length=16)


class StorageTierConsolidateWarmBody(BaseModel):
    """Consolidate chunks from a source into warm-tier summaries."""

    source_id: str = Field(..., min_length=1)


class StorageTierOffloadColdBody(BaseModel):
    """Offload warm chunks to cold tier (sparse file storage)."""

    source_id: str = Field(..., min_length=1)


class ChunkDeduplicateBody(BaseModel):
    """Deduplicate chunks by content fingerprint."""

    min_chunk_age_days: float = Field(default=7.0, ge=1.0, le=365.0)
    dry_run: bool = True


class DestructiveConfirmBody(BaseModel):
    confirm: bool = False


def _surface_db_rotate_flag(data_dir: Path, conn) -> bool:
    """If SQLite recovery rotated the DB aside, surface one Activity health issue.

    Returns True when a fresh rotation was found and surfaced, so the caller can
    decide whether to *skip* the automatic inbox re-index. Blindly replaying a
    large inbox onto a just-emptied DB is exactly what turned a single
    corruption into a CPU-pinning re-ingest loop, so a rotation pauses the
    auto-reindex until the user (or the next clean restart) opts back in.
    """
    flag = data_dir / ".last_db_rotate.json"
    if not flag.exists():
        return False
    try:
        meta = json.loads(flag.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        meta = {}
    backup = str(meta.get("backup_path") or "see data folder")
    system_issue_upsert(
        conn,
        issue_id="db_rotate",
        severity="elevated",
        source_key="db_rotate",
        body_md=(
            "Your vault database was replaced after corruption was detected. "
            f"Backup: `{backup}`. Life-graph answers may also be in `graph_snapshot.json`. "
            "Automatic re-indexing of the inbox was paused to avoid reload churn; "
            "it resumes on the next restart, or trigger it now from Settings."
        ),
    )
    try:
        flag.unlink()
    except OSError:
        pass
    return True


@app.post("/nuke")
def nuke_db(body: DestructiveConfirmBody = DestructiveConfirmBody()) -> Dict[str, Any]:
    """Delete the local memory database and related runtime artifacts.

    Intended for "factory reset" / clean-slate behaviour. The desktop app
    should restart the sidecar after calling this. Requires ``{"confirm": true}``.
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail='destructive action requires JSON body {"confirm": true}',
        )
    removed: List[str] = []
    missing: List[str] = []

    candidates = [
        State.db_path,
        State.data_dir / "telemetry.jsonl",
        State.data_dir / "telemetry.jsonl.1",
        State.data_dir / ".staging",
    ]
    for p in candidates:
        try:
            if not p.exists():
                missing.append(str(p))
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(str(p))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to remove {p}: {e.__class__.__name__}: {e}")

    # Ensure the directory still exists (so the next boot can recreate db).
    try:
        State.data_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to ensure data_dir: {e.__class__.__name__}: {e}")

    # Drop any cached per-thread sqlite connection so a future request
    # can't keep using a deleted DB file handle.
    try:
        c = getattr(State._tls, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            State._tls.conn = None
    except Exception:
        pass

    return {"removed": removed, "missing": missing, "db_path": str(State.db_path)}


@app.post("/factory-reset")
def factory_reset(body: DestructiveConfirmBody = DestructiveConfirmBody()) -> Dict[str, Any]:
    """More aggressive reset than /nuke.

    Deletes the database *and* clears the inbox directory contents.
    The desktop app should restart the sidecar after calling this.
    Requires ``{"confirm": true}``.
    """
    result = nuke_db(body)
    inbox_removed: List[str] = []
    inbox_missing: List[str] = []

    try:
        if not State.inbox.exists():
            inbox_missing.append(str(State.inbox))
        else:
            # Remove children, not the inbox dir itself (so watchers/UX stay stable).
            for child in list(State.inbox.iterdir()):
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    inbox_removed.append(str(child))
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"failed to clear inbox item {child}: {e.__class__.__name__}: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to clear inbox: {e.__class__.__name__}: {e}")

    return {
        **result,
        "inbox": str(State.inbox),
        "inbox_removed": inbox_removed,
        "inbox_missing": inbox_missing,
    }


@app.get("/settings")
def settings_endpoint() -> Dict[str, Any]:
    data = load_settings(State.data_dir)
    return {"settings": data, "all_kinds": list(ALL_KINDS)}


@app.put("/settings")
def update_settings(body: SettingsBody) -> Dict[str, Any]:
    current = load_settings(State.data_dir)
    if body.disabled_kinds is not None:
        current["disabled_kinds"] = body.disabled_kinds
    if body.telemetry_opt_out is not None:
        current["telemetry_opt_out"] = bool(body.telemetry_opt_out)
    if body.remote_monitoring is not None:
        current["remote_monitoring"] = bool(body.remote_monitoring)
    if body.ambient_sensing_enabled is not None:
        current["ambient_sensing_enabled"] = bool(body.ambient_sensing_enabled)
    if body.full_listening_enabled is not None:
        fl = bool(body.full_listening_enabled)
        current["full_listening_enabled"] = fl
        merged_ac = dict(current.get("ambient_collectors") or {})
        merged_ac["full_listening"] = fl
        current["ambient_collectors"] = merged_ac
    if body.ambient_collectors is not None:
        merged = dict(current.get("ambient_collectors") or {})
        merged.update({k: bool(v) for k, v in body.ambient_collectors.items()})
        if "full_listening" in body.ambient_collectors:
            current["full_listening_enabled"] = bool(body.ambient_collectors["full_listening"])
        current["ambient_collectors"] = merged
    if body.capture_on_empty_ax is not None:
        current["capture_on_empty_ax"] = bool(body.capture_on_empty_ax)
    if body.ambient_deny is not None:
        current["ambient_deny"] = body.ambient_deny
    saved = save_settings(State.data_dir, current)
    apply_settings(saved)
    return {"settings": saved, "all_kinds": list(ALL_KINDS)}


@app.get("/settings/consent")
def get_consent_settings(profile_id: Optional[str] = None) -> Dict[str, Any]:
    """Effective MCP/data-sharing consent policy persisted under consent_policy.json."""
    if profile_id:
        return consent_policy.load_policy_for_profile(State.data_dir, profile_id)
    return consent_policy.load_policy(State.data_dir)


@app.put("/settings/consent")
def put_consent_settings(
    body: Dict[str, Any] = Body(...),
    profile_id: Optional[str] = None,
) -> Dict[str, Any]:
    conn = State.conn()
    if profile_id:
        readers = body.get("readers")
        if not isinstance(readers, dict):
            raise HTTPException(status_code=400, detail="body.readers object required for profile-scoped save")
        policy = consent_policy.save_policy_for_profile(State.data_dir, profile_id, readers)
    else:
        consent_policy.save_policy(State.data_dir, body)
        policy = consent_policy.load_policy(State.data_dir)
    identity_audit_log_append(
        conn,
        action="consent_policy_put",
        detail={
            "schema_version": body.get("schema_version"),
            "profile_id": profile_id,
        },
    )
    conn.commit()
    return {"ok": True, "policy": policy, "profile_id": profile_id}


@app.post("/reconcile")
def reconcile_endpoint(body: ReconcileBody) -> Dict[str, Any]:
    """Full inbox scan → DB (and optional force re-embed). Runs in the background."""
    if not _manual_reconcile_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="reconcile already running")
    force = body.force

    def _run() -> None:
        try:
            from watcher import reconcile_once

            conn = connect(State.db_path)
            try:
                reconcile_once(
                    conn,
                    State.inbox,
                    force=force,
                    on_event=_watcher_event_bridge,
                )
            finally:
                conn.close()
        except Exception:
            log.exception("manual reconcile failed")
        finally:
            _manual_reconcile_lock.release()

    threading.Thread(target=_run, name="minion-api-reconcile-manual", daemon=True).start()
    return {"started": True, "force": force}


@app.get("/status")
def status() -> Dict[str, Any]:
    active = _public_active()
    conn = State.conn()
    from store import profile_get_active

    profile_id = profile_get_active(conn)
    return {
        "version": __version__,
        "data_dir": str(State.data_dir),
        "inbox": str(State.inbox),
        "db_path": str(State.db_path),
        "supported_extensions": supported_extensions(),
        "counts": _counts(profile_id),
        "active_profile_id": profile_id,
        "active": active,
        "database": _database_status(),
        "watcher": {
            "running": _watcher_thread is not None and _watcher_thread.is_alive()
            if _watcher_thread
            else False,
            "mode": _watcher_mode,
        },
    }


@app.get("/capabilities")
def capabilities() -> Dict[str, Any]:
    """Lightweight contract for non-MCP local clients (same host as this API)."""
    tok_on = bool(os.environ.get("MINION_API_TOKEN", "").strip())
    return {
        "service": "minion-api",
        "product": "minion",
        "version": __version__,
        "schema_version": 1,
        "auth": {
            "mutation_bearer": tok_on,
            "scheme": "Bearer",
            "header": "Authorization",
            "policy": "Loopback GET and POST /search require no token; LAN API routes require Authorization: Bearer <MINION_API_TOKEN> when set. POST /mcp uses the MCP password.",
            "mcp_http": {
                "endpoint": "POST /mcp",
                "password_env": "MINION_MCP_HTTP_TOKEN",
                "default_password": "foofie",
                "policy": "Send Authorization: Bearer <password> or X-Minion-Password.",
            },
        },
        "retrieval": {
            "identity_bias": True,
            "rrf_fusion": True,
            "mcp_consent": {
                "policy_file": "consent_policy.json",
                "note": (
                    "Desktop HTTP search stays full vault; MCP ask_minion drops chunks matching "
                    "readers.mcp.deny_chunk_source_kinds / deny_path_substrings."
                ),
            },
        },
        "analytics": {
            "url_configured": bool(analytics_remote.effective_analytics_url().strip()),
            "telemetry_opt_out": bool(load_settings(State.data_dir).get("telemetry_opt_out")),
            "opt_out_setting": "telemetry_opt_out in settings.json (PUT /settings); default false = sends anonymized summaries.",
            "note": (
                "POST bodies omit queries, paths, and secrets. Your HTTP server still sees "
                "client IP, User-Agent, and TLS metadata like any public endpoint — disclose that."
            ),
        },
        "endpoints": {
            "search": "POST /search",
            "mcp": "POST /mcp",
            "search_stream": "GET /search/stream",
            "ingest": "POST /ingest",
            "delete_sources_bulk": "DELETE /sources body {kind, confirm_bulk:true}",
            "ingest_webhook": "POST /ingest/webhook",
            "extensions": "GET /extensions",
            "extensions_reload": "POST /extensions/reload",
            "reconcile": "POST /reconcile",
            "events_ws": "WS /events",
            "identity_claims": "GET /identity/claims",
            "identity_mirror": "GET /identity/mirror",
            "identity_propose": "POST /identity/claims/propose",
            "identity_export": "POST /identity/export",
            "clusters_rebuild": "POST /identity/clusters/rebuild",
            "settings_consent_get": "GET /settings/consent",
            "settings_consent_put": "PUT /settings/consent",
            "ambient_events": "GET /ambient/events",
            "feed": "GET /feed",
            "graph_scaffold": "GET /graph/scaffold",
            "graphify_status": "GET /graphify/status",
            "graphify_shadow_build": "POST /graphify/shadow-build",
            "graphify_reconcile": "POST /graphify/reconcile",
            "storage_report": "POST /maintenance/storage-report",
            "storage_tier_promote_stale": "POST /maintenance/storage-tier-promote-stale",
            "diagnostics_about": "GET /diagnostics/about",
            "diagnostics_log": "GET /diagnostics/log",
            "diagnostics_log_text": "GET /diagnostics/log/text",
            "diagnostics_log_stream": "GET /diagnostics/log/stream",
            "diagnostics_peers": "GET /diagnostics/peers",
        },
    }


@app.get("/diagnostics/about")
def diagnostics_about() -> Dict[str, Any]:
    """Static copy for the Support pane; safe to cache in the UI."""
    return {
        "name": "Minion",
        "tagline": "Private, searchable long-term memory for AI assistants — local SQLite, drop-zone ingest, MCP for Claude.",
        "license": "MIT",
        "homepage": "https://github.com/reif-is-a-foofie/Minion",
        "privacy": (
            "Diagnostics routes are GET-only and meant for 127.0.0.1. "
            "Minion does not upload logs or telemetry to us automatically. "
            "Log lines may still contain filenames you indexed; use redacted export when sharing."
        ),
    }


@app.post("/diagnostics/client-log")
def diagnostics_client_log(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Accept an error/crash report from the desktop UI and forward it to the
    collector **iff** the user opted into remote monitoring. Always returns
    ``{forwarded: bool}`` so the client never needs to know the setting state.

    Used to surface UI crashes (e.g. the WebView renderer dying → white screen)
    that the sidecar can't observe on its own."""
    body = body or {}
    message = str(body.get("message") or "client error")[:600]
    detail = body.get("detail")
    context = body.get("context") if isinstance(body.get("context"), dict) else None
    src = str(body.get("source") or "desktop")[:24]
    forwarded = False
    try:
        root = analytics_remote.telemetry_data_dir()
        if root is not None:
            ok, _url = analytics_remote._monitoring_enabled(root)
            if ok:
                analytics_remote.emit_error(
                    src,
                    message,
                    detail=str(detail) if detail else None,
                    context=context,
                )
                forwarded = True
    except Exception:
        log.debug("client-log forward failed", exc_info=True)
    return {"forwarded": forwarded}


@app.get("/diagnostics/log")
def diagnostics_log(lines: int = 200) -> Dict[str, Any]:
    """Return the tail of ``MINION_LOG_FILE`` with best-effort redaction."""
    n = max(1, min(2500, int(lines)))
    path, log_lines = diagnostics.read_log_tail(max_lines=n)
    hint = str(path) if path else None
    if hint and path is not None:
        try:
            home = os.path.expanduser("~")
            if home and len(home) > 2:
                hint = hint.replace(home, "~")
        except Exception:
            pass
    return {
        "log_file_hint": hint,
        "lines": log_lines,
        "count": len(log_lines),
    }


@app.get("/diagnostics/log/text")
def diagnostics_log_text(lines: int = 200) -> PlainTextResponse:
    """Plain newline-separated tail for pasting into email or tickets."""
    body = diagnostics_log(lines=lines)
    text = "\n".join(body.get("lines") or [])
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")


@app.get("/diagnostics/log/stream")
def diagnostics_log_stream() -> StreamingResponse:
    """SSE stream of redacted log lines (blocks a worker thread while connected)."""

    return StreamingResponse(
        diagnostics.iter_log_sse_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/diagnostics/peers")
def diagnostics_peers(request: Request) -> Dict[str, Any]:
    """Find other Minion HTTP APIs on loopback (separate app instances / tests)."""
    my_port = request.url.port
    if my_port is None:
        try:
            my_port = int(os.environ.get("MINION_API_PORT", "0") or 0)
        except ValueError:
            my_port = 0
    lo = int(os.environ.get("MINION_PEER_SCAN_PORT_LO", "8688") or 8688)
    hi = int(os.environ.get("MINION_PEER_SCAN_PORT_HI", "8799") or 8799)
    rows = diagnostics.discover_minion_peers(my_port if my_port else None, port_lo=lo, port_hi=hi)
    return {"instances": rows, "scan": {"port_lo": lo, "port_hi": hi}}


@app.get("/extensions")
def extensions_get() -> Dict[str, Any]:
    """Describe user parser mappings + webhook ingest (no secrets)."""
    return {
        "manifest_path": str(manifest_path(State.data_dir)),
        "user_extensions": [
            {"suffix": k, "kind": v[0], "module": v[1], "function": v[2]}
            for k, v in sorted(user_extension_mappings().items())
        ],
        "supported_extensions": supported_extensions(),
        "parser_manifest_schema": {
            "version": 1,
            "extensions": [
                {
                    "suffix": ".proto",
                    "kind": "code",
                    "module": "parsers.code",
                    "function": "parse",
                }
            ],
            "note": "module must start with parsers. — maps new suffixes to in-tree parsers only.",
        },
        "ingest_webhook": {
            "method": "POST",
            "path": "/ingest/webhook",
            "json_body": {
                "source_key": "stable id (e.g. slack:channel-123)",
                "display_name": "optional",
                "kind": "external | text | … (must be a known ALL_KINDS value)",
                "parser": "webhook (default)",
                "chunks": [{"text": "…", "role": null, "meta": {}}],
            },
            "ndjson": {
                "content_type": "application/x-ndjson",
                "query": "source_key required",
                "lines": 'each line JSON: {"text":"…","role":null,"meta":{}}',
            },
            "auth": "Bearer MINION_API_TOKEN when MINION_API_TOKEN is set",
        },
    }


@app.post("/extensions/reload")
def extensions_reload() -> Dict[str, Any]:
    """Re-read ``parser_extensions.json`` from the data directory."""
    n = load_user_extensions(State.data_dir)
    return {"reloaded": n, "manifest_path": str(manifest_path(State.data_dir))}


@app.get("/sources")
def list_sources_endpoint(
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 500,
    source_type: Optional[str] = None,
    time_range: Optional[str] = None,
) -> Dict[str, Any]:
    conn = State.conn()
    from store import profile_get_active

    profile_id = profile_get_active(conn)
    rows = list_sources(
        conn,
        kind=kind,
        path_glob=path_glob,
        since=since,
        limit=limit,
        source_type=source_type,
        time_range=time_range,
        profile_id=profile_id,
    )
    return {"sources": rows, "counts": _counts(profile_id)}


@app.get("/sources/reveal-path")
def reveal_path(path: str) -> Dict[str, Any]:
    """Resolve the actual path to reveal in Finder.
    
    If the source.path exists, use it. Otherwise, look up the original path
    from file_tracking.jsonl (for temporary ingests where the inbox copy was deleted).
    """
    from file_tracker import find_original_path
    
    p = Path(path).expanduser().resolve()
    if p.exists():
        return {"reveal_path": str(p), "resolved_via": "direct"}
    
    # Path doesn't exist - try to find original from file_tracking.jsonl
    original = find_original_path(State.data_dir, p)
    if original:
        orig_path = Path(original).expanduser().resolve()
        if orig_path.exists():
            return {"reveal_path": str(orig_path), "resolved_via": "file_tracking"}
        else:
            return {
                "reveal_path": str(orig_path),
                "resolved_via": "file_tracking",
                "exists": False,
                "error": "original file also missing",
            }
    
    return {
        "reveal_path": str(p),
        "resolved_via": "none",
        "exists": False,
        "error": "path not found and no tracking record",
    }


@app.get("/sources/{source_id}")
def source_info(source_id: str) -> Dict[str, Any]:
    src = get_source(State.conn(), source_id)
    if src is None:
        raise HTTPException(status_code=404, detail=f"source_id not found: {source_id}")
    conn = State.conn()
    cc = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (source_id,)
    ).fetchone()["n"]
    return {
        "source_id": src.source_id,
        "path": src.path,
        "kind": src.kind,
        "sha256": src.sha256,
        "mtime": src.mtime,
        "bytes": src.bytes,
        "parser": src.parser,
        "updated_at": src.updated_at,
        "chunk_count": int(cc),
        "meta": src.meta,
    }


@app.delete("/sources")
def delete_endpoint(body: DeleteBody) -> Dict[str, Any]:
    conn = State.conn()
    if body.kind is not None:
        n_src, n_chunks = delete_sources_by_kind(conn, body.kind.strip())
        _schedule_broadcast(
            {
                "type": "sources_bulk_removed",
                "kind": body.kind.strip(),
                "sources_removed": n_src,
                "counts": _counts(),
            }
        )
        return {"removed_chunks": n_chunks, "sources_removed": n_src, "kind": body.kind.strip()}
    if body.source_id:
        n = delete_source(conn, body.source_id)
        key = body.source_id
    else:
        assert body.path is not None
        p = str(Path(body.path).expanduser().resolve())
        n = delete_source_by_path(conn, p)
        key = p
    _schedule_broadcast({"type": "source_removed", "key": key, "counts": _counts()})
    return {"removed_chunks": n}


_query_model = None
_query_model_name: Optional[str] = None
_query_model_lock = threading.Lock()


def _get_query_model():
    global _query_model, _query_model_name
    with _query_model_lock:
        if _query_model is not None:
            return _query_model
        from fastembed import TextEmbedding
        from store import get_meta

        from ingest import DEFAULT_MODEL

        name = (
            get_meta(State.conn(), "model_name")
            or os.environ.get("MINION_EMBED_MODEL")
            or DEFAULT_MODEL
        )
        _query_model_name = name
        _query_model = TextEmbedding(
            model_name=name, cache_dir=fastembed_cache_dir(data_dir=State.data_dir)
        )
        return _query_model


def _embed_search_results(
    query: str,
    top_k: int,
    kind: Optional[str],
    path_glob: Optional[str],
    since: Optional[float],
    role: Optional[str],
    max_chars: int,
    profile_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    conn = State.conn()
    if profile_id is None:
        from store import profile_get_active

        profile_id = profile_get_active(conn)
    model = _get_query_model()
    from ingest import apply_query_prefix

    text = apply_query_prefix(_query_model_name or "", query)
    vec = np.asarray(next(iter(model.embed([text]))), dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    internal_k = max(top_k * 3, top_k + 8)
    relevance_hits = store_search(
        conn,
        vec,
        top_k=internal_k,
        kind=kind,
        path_glob=path_glob,
        since=since,
        role=role,
        profile_id=profile_id,
    )
    hits = relevance_hits
    rerank_used = "none"
    if query and fts_available(conn):
        try:
            keyword_hits = store_keyword_search(
                conn,
                query,
                top_k=internal_k,
                role=role,
                kind=kind,
                path_glob=path_glob,
                profile_id=profile_id,
            )
            if keyword_hits:
                hits = rrf_fuse(relevance_hits, keyword_hits)
                rerank_used = "rrf"
        except Exception:
            log.exception("RRF fusion failed; relevance-only")
    # Pure relevance only. Identity/graph reranking is a separate surface so it
    # can't degrade retrieval (see mcp_server._tool_ask_minion + retrieval_bias).
    bias_meta: Dict[str, Any] = {}
    hits = hits[:top_k]

    results: List[Dict[str, Any]] = []
    for h in hits:
        text = h.text
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        results.append(
            {
                "score": round(h.score, 4),
                "chunk_id": h.chunk_id,
                "role": h.role,
                "source_id": h.source_id,
                "path": h.path,
                "kind": h.kind,
                "mtime": h.mtime,
                "text": text,
                "meta": h.meta,
                "storage_tier": getattr(h, "storage_tier", None) or "hot",
            }
        )
    try:
        top = results[0] if results else {}
        telemetry.log_event(
            "search",
            mode="relevance",
            query=query or None,
            top_k=top_k,
            returned=len(results),
            top_score=top.get("score"),
            top_path=top.get("path"),
            top_kind=top.get("kind"),
            rerank=rerank_used,
            candidates=len(relevance_hits),
            content_dropped=None,
            hit_kinds=[r.get("kind") for r in results],
            kind_filter=kind,
            path_glob=path_glob,
            role=role,
            bias_clusters=bias_meta.get("bias_clusters"),
            bias_claims=bias_meta.get("bias_claims"),
            bias_run_at=bias_meta.get("bias_run_at"),
            adjustments_applied=bias_meta.get("adjustments_applied"),
            tier_bias_non_hot=bias_meta.get("tier_bias_non_hot"),
        )
    except Exception:
        pass
    try:
        from graph_corpus_mine import schedule_background_graph_mine

        schedule_background_graph_mine(State.data_dir, query=query)
    except Exception:
        log.debug("query graph mine schedule skipped", exc_info=True)
    return results


@app.post("/search")
def search_endpoint(body: SearchBody) -> Dict[str, Any]:
    return {
        "results": _embed_search_results(
            body.query,
            body.top_k,
            body.kind,
            body.path_glob,
            body.since,
            body.role,
            body.max_chars,
            profile_id=body.profile_id,
        )
    }


@app.get("/search/stream")
def search_stream(
    query: str,
    top_k: int = 8,
    kind: Optional[str] = None,
    path_glob: Optional[str] = None,
    role: Optional[str] = None,
    since: Optional[float] = None,
    max_chars: int = 600,
) -> StreamingResponse:
    """Server-Sent Events stream of semantic search hits (one `hit` event per result)."""

    def gen():
        try:
            rows = _embed_search_results(
                query, top_k, kind, path_glob, since, role, max_chars
            )
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
            return
        yield f"event: meta\ndata: {json.dumps({'count': len(rows), 'query': query})}\n\n"
        for row in rows:
            yield f"event: hit\ndata: {json.dumps(row)}\n\n"
        yield f"event: done\ndata: {json.dumps({})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/identity/claims")
def identity_claims_list(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    rows, err = identity.list_claims(State.conn(), status=status, kind=kind, limit=limit)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"claims": rows, "count": len(rows)}


@app.get("/identity/claims/{claim_id}")
def identity_claim_detail(claim_id: str) -> Dict[str, Any]:
    row = identity_claim_get(State.conn(), claim_id)
    if row is None:
        raise HTTPException(status_code=404, detail="claim not found")
    return {"claim": row}


@app.get("/identity/claims/{claim_id}/edges")
def identity_claim_edges(claim_id: str) -> Dict[str, Any]:
    if identity_claim_get(State.conn(), claim_id) is None:
        raise HTTPException(status_code=404, detail="claim not found")
    edges = identity_edges_for_claim(State.conn(), claim_id)
    return {"edges": edges, "count": len(edges)}


@app.post("/identity/claims/propose")
def identity_propose(body: IdentityProposeBody) -> Dict[str, Any]:
    payload, err = identity.propose_identity_update(
        State.conn(),
        kind=body.kind,
        text=body.text,
        source_agent=body.source_agent,
        confidence=body.confidence,
        evidence_chunk_ids=body.evidence_chunk_ids,
        evidence_rationales=body.evidence_rationales,
        meta=body.meta,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    assert payload is not None
    telemetry.log_event("identity_propose", claim_id=payload.get("claim_id"))
    conn = State.conn()
    identity_audit_log_append(
        conn,
        action="identity_propose",
        claim_id=payload.get("claim_id"),
        detail={"kind": body.kind, "source_agent": body.source_agent},
    )
    conn.commit()
    return payload


@app.patch("/identity/claims/{claim_id}")
def identity_patch_claim(claim_id: str, body: IdentityPatchBody) -> Dict[str, Any]:
    conn = State.conn()
    meta_merge: Optional[Dict[str, Any]] = None
    if body.meta is not None or body.revision_source is not None:
        meta_merge = dict(body.meta or {})
        if body.revision_source:
            rs = body.revision_source.strip()[:64]
            if rs:
                meta_merge["revision_source"] = rs
        if not meta_merge:
            meta_merge = None
    row, err = identity.patch_claim(
        conn,
        claim_id,
        status=body.status,
        superseded_by=body.superseded_by,
        text=body.text,
        meta_merge=meta_merge,
    )
    if err:
        raise HTTPException(status_code=404 if "not found" in (err or "") else 400, detail=err)
    if row is None:
        raise HTTPException(status_code=404, detail="claim not found")
    identity_audit_log_append(
        conn,
        action="identity_patch",
        claim_id=claim_id,
        detail={
            "status": body.status,
            "superseded_by": body.superseded_by,
            "revision_source": (meta_merge or {}).get("revision_source"),
        },
    )
    conn.commit()
    telemetry.log_event(
        "identity_patch",
        claim_id=claim_id,
        status=body.status or row.get("status"),
    )
    return {"claim": identity_claim_get(conn, claim_id)}


@app.get("/identity/summary")
def identity_summary(include_evidence: bool = True) -> Dict[str, Any]:
    return {"markdown": identity.build_identity_summary(State.conn(), include_evidence=include_evidence)}


@app.get("/identity/mirror")
def identity_mirror(limit_history: int = 60) -> Dict[str, Any]:
    """Time-aware mirror: digest markdown plus superseded/rejected claim history."""
    conn = State.conn()
    lim = int(max(1, min(limit_history, 500)))
    hist = identity_claim_mirror_history(conn, limit=lim)
    md = identity.build_identity_summary(conn, history_tail=min(12, lim))
    return {"markdown": md, "history": hist, "history_count": len(hist)}


@app.get("/identity/history")
def identity_history(
    claim_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Revision history for identity claims with supersession tracking."""
    conn = State.conn()
    lim = int(max(1, min(limit, 500)))
    
    if claim_id:
        claim = identity_claim_get(conn, claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="claim not found")
        # Get all claims that supersede or are superseded by this claim
        rows = []
        if claim.get("superseded_by"):
            sup = identity_claim_get(conn, claim["superseded_by"])
            if sup:
                rows.append(sup)
        # Find claims that were superseded by this one
        superseded = conn.execute(
            "SELECT claim_id, kind, text, status, confidence, source_agent, "
            "created_at, updated_at, superseded_by, superseded_at, meta_json "
            "FROM identity_claims WHERE superseded_by=? ORDER BY updated_at DESC",
            (claim_id,),
        ).fetchall()
        rows.extend([_row_identity_claim(r) for r in superseded])
        rows.append(claim)
    else:
        # Return history filtered by status (default to superseded/rejected)
        filter_status = status if status in ("superseded", "rejected") else None
        if filter_status:
            rows = identity_claim_list(conn, status=filter_status, limit=lim)
        else:
            rows = identity_claim_mirror_history(conn, limit=lim)
    
    return {"history": rows, "count": len(rows)}


@app.post("/identity/revert")
def identity_revert(body: Dict[str, Any]) -> Dict[str, Any]:
    """Revert to a previous identity claim version."""
    claim_id = body.get("claim_id")
    if not claim_id:
        raise HTTPException(status_code=400, detail="claim_id required")
    
    conn = State.conn()
    claim = identity_claim_get(conn, claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="claim not found")
    
    if claim["status"] not in ("superseded", "rejected"):
        raise HTTPException(status_code=400, detail="can only revert superseded or rejected claims")
    
    # Find the claim that superseded this one
    superseded_by = claim.get("superseded_by")
    if not superseded_by:
        raise HTTPException(status_code=400, detail="claim was not superseded by another claim")
    
    current_claim = identity_claim_get(conn, superseded_by)
    if not current_claim:
        raise HTTPException(status_code=404, detail="superseding claim not found")
    
    # Revert: set old claim to active, clear superseded fields
    # Set current claim to superseded, point it back to the reverted claim
    identity_claim_patch_fields(
        conn,
        claim_id,
        status="active",
        superseded_by=None,
        meta_merge={"revision_source": "user_revert", "reverted_at": time.time()},
    )
    
    identity_claim_patch_fields(
        conn,
        superseded_by,
        status="superseded",
        superseded_by=claim_id,
        meta_merge={"revision_source": "user_revert", "reverted_at": time.time()},
    )
    
    identity_audit_log_append(
        conn,
        action="identity_revert",
        claim_id=claim_id,
        detail={
            "reverted_from": superseded_by,
            "previous_status": claim["status"],
            "author": "user",
        },
    )
    
    conn.commit()
    
    return {
        "ok": True,
        "reverted_claim": identity_claim_get(conn, claim_id),
        "superseded_claim": identity_claim_get(conn, superseded_by),
    }


@app.get("/identity/companion")
def identity_companion_route() -> Dict[str, Any]:
    from identity_companion import companion_overview

    return companion_overview(State.conn(), State.data_dir)


@app.post("/identity/companion/start")
def identity_companion_start_route() -> Dict[str, Any]:
    from identity_companion import open_companion_thread

    out = open_companion_thread(State.conn())
    State.conn().commit()
    return out


@app.get("/identity/clusters")
def identity_clusters() -> Dict[str, Any]:
    rows = preference_clusters_list(State.conn())
    return {"clusters": rows, "count": len(rows)}


@app.post("/identity/clusters/rebuild")
def identity_clusters_rebuild(body: ClusterRebuildBody) -> Dict[str, Any]:
    try:
        out = run_preference_clustering(
            State.conn(),
            sample_limit=body.sample_limit,
            k=body.k,
            use_llm=body.use_llm,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")
    State.conn().commit()
    return out


@app.get("/ambient/events")
def ambient_events_list(limit: int = 80) -> Dict[str, Any]:
    rows = ambient_events_recent(State.conn(), limit=limit)
    return {"events": rows, "count": len(rows)}


@app.get("/attention/summary")
def attention_summary(hours: float = 24.0) -> Dict[str, Any]:
    from attention_rollup import rollup_attention

    since = time.time() - max(0.5, min(hours, 168.0)) * 3600.0
    return rollup_attention(State.conn(), since_ts=since, limit=800)


class WikiPageBody(BaseModel):
    page_type: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=500)
    body_md: str = ""
    status: str = "active"
    meta: Dict[str, Any] = Field(default_factory=dict)
    page_id: Optional[str] = None


class WikiLinkBody(BaseModel):
    to_page_id: str
    link_kind: str = "related"


class TaskInferBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    body_md: str = ""
    origin: str = "agent"
    priority: Optional[str] = None
    context_refs: List[Any] = Field(default_factory=list)
    wiki_refs: List[str] = Field(default_factory=list)


class TaskPatchBody(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    body_md: Optional[str] = None


class OutputBody(BaseModel):
    kind: str = "draft"
    body_md: str = ""
    wiki_read: List[str] = Field(default_factory=list)


class OutputPatchBody(BaseModel):
    status: Optional[str] = None
    body_md: Optional[str] = None


class AmbientIndexAxBody(BaseModel):
    dry_run: bool = False


class ScreenRememberBody(BaseModel):
    max_lines: int = Field(default=1200, ge=1, le=50_000)
    ingest_screenshots: bool = True
    run_adapters: bool = True


class ScreenCreateTaskBody(BaseModel):
    minutes: int = Field(default=20, ge=1, le=24 * 60)
    title: str = ""


class GraphCandidateResolveBody(BaseModel):
    status: str = Field(pattern="^(approved|rejected|dismissed|merged)$")
    payload: Optional[Dict[str, Any]] = None


class GraphExtractBody(BaseModel):
    source_ids: Optional[List[str]] = None


class GraphReshuffleBody(BaseModel):
    force: bool = False


@app.get("/today")
def today_bundle() -> Dict[str, Any]:
    return build_today_bundle(State.conn(), State.data_dir)


@app.get("/feed")
def activity_feed(
    limit: int = 80,
    since_hours: float = 48.0,
    profile_id: Optional[str] = None,
) -> Dict[str, Any]:
    return build_activity_feed(
        State.conn(),
        State.data_dir,
        limit=limit,
        since_hours=since_hours,
        profile_id=profile_id,
    )


@app.post("/life-evidence/refresh")
def life_evidence_refresh(force: bool = False) -> Dict[str, Any]:
    """Refresh Contacts/Calendar snapshots, then index them into the graph."""
    from life_evidence_index import ingest_life_evidence
    from life_evidence_snapshot import refresh_life_evidence_if_stale

    max_age = 0.0 if force else 6 * 3600
    out = refresh_life_evidence_if_stale(State.data_dir, max_age_sec=max_age)
    indexed = ingest_life_evidence(State.data_dir, State.conn())
    out["indexed_contacts"] = indexed.get("contacts", 0)
    out["indexed_calendar"] = indexed.get("calendar", 0)
    State.conn().commit()
    return out


@app.post("/life-evidence/ingest")
def life_evidence_ingest() -> Dict[str, Any]:
    """Index existing life_evidence JSON snapshots into the graph."""
    from life_evidence_index import ingest_life_evidence

    out = ingest_life_evidence(State.data_dir, State.conn())
    State.conn().commit()
    return {"indexed_contacts": out.get("contacts", 0), "indexed_calendar": out.get("calendar", 0)}


class CouncilApproveBody(BaseModel):
    proposal_id: str
    action: str
    edited_payload: Optional[Dict[str, Any]] = None
    snooze_days: Optional[float] = None


@app.post("/council/approve")
def council_approve(body: CouncilApproveBody) -> Dict[str, Any]:
    return handle_council_approve(
        State.conn(),
        proposal_id=body.proposal_id,
        action=body.action,
        edited_payload=body.edited_payload,
        snooze_days=body.snooze_days,
    )


@app.get("/audit")
def audit_log(entity_type: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    """Retrieve unified audit log for identity and graph changes."""
    logs = audit_log_list(State.conn(), entity_type=entity_type, limit=limit)
    return {"logs": logs, "count": len(logs)}


@app.post("/audit/{audit_id}/rollback")
def audit_rollback(audit_id: int) -> Dict[str, Any]:
    """Rollback a specific audit log entry (identity claim revert)."""
    conn = State.conn()
    
    # Find the audit entry
    row = conn.execute(
        "SELECT entity_type, entity_id, action, detail_json FROM identity_audit_log WHERE id=?",
        (audit_id,),
    ).fetchone()
    
    if not row:
        raise HTTPException(status_code=404, detail="Audit entry not found")
    
    entity_type = row["entity_type"]
    entity_id = row["entity_id"]
    action = row["action"]
    detail = json.loads(row["detail_json"] or "{}")
    
    # Only support identity claim rollback for now
    if entity_type != "identity" or not entity_id:
        return {"ok": False, "error": "Rollback only supported for identity claims"}
    
    try:
        from store import identity_claim_patch_fields
        identity_claim_patch_fields(
            conn,
            claim_id=entity_id,
            status="proposed",
            meta_merge={"revision_source": "audit_rollback", "rolled_back_at": time.time()},
        )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/graph/communities")
def graph_communities() -> Dict[str, Any]:
    """L4 global index: the persisted community summaries over the knowledge graph."""
    from graph_community import get_community_index

    return {"communities": get_community_index(State.conn())}


@app.post("/graph/communities/rebuild")
def graph_communities_rebuild() -> Dict[str, Any]:
    from graph_community import build_communities

    conn = State.conn()
    result = build_communities(conn, State.data_dir)
    conn.commit()
    return result


@app.post("/graph/build")
def graph_build() -> Dict[str, Any]:
    """Manual 'Build graph now': corpus-agnostic eager mine of the user's own
    entities across the whole corpus, then L4 communities. Runs in the
    background (debounced); returns immediately. No-op without an LLM key."""
    from graph_corpus_mine import schedule_corpus_graph_build

    return schedule_corpus_graph_build(State.data_dir, delay=0.5)


@app.get("/graph/stats")
def graph_stats() -> Dict[str, Any]:
    """Cheap counts for the dashboard: real (non-scaffold) nodes, edges,
    L4 communities, and whether a build is in progress."""
    conn = State.conn()
    from store import graph_active_profile_id

    active_profile = graph_active_profile_id(conn)
    profile_clause, profile_params = (
        " AND COALESCE(profile_id, 'default') = ?",
        [active_profile],
    )

    def _count(sql: str, params: Optional[List[Any]] = None) -> int:
        try:
            row = conn.execute(sql, params or []).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    nodes = _count(
        "SELECT COUNT(*) FROM graph_nodes WHERE status NOT IN ('scaffold', 'stub')"
        + profile_clause,
        profile_params,
    )
    edges = _count("SELECT COUNT(*) FROM graph_edges")
    communities = _count(
        "SELECT COUNT(*) FROM sources WHERE kind='graph-community'"
    )
    try:
        from graph_corpus_mine import corpus_build_running

        building = corpus_build_running()
    except Exception:
        building = False

    embed_dim = 0
    embed_model = ""
    try:
        from store import get_embed_dim

        embed_dim = get_embed_dim(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='model_name'").fetchone()
        embed_model = str(row[0]) if row else ""
    except Exception:
        pass

    return {
        "nodes": nodes,
        "edges": edges,
        "communities": communities,
        "building": building,
        "embed_dim": embed_dim,
        "embed_model": embed_model,
        "active_profile_id": active_profile,
    }


@app.post("/admin/reindex")
def admin_reindex(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Re-embed the corpus under the current default model in a background
    thread. Heavy; a restart is recommended once it completes. The live DB is
    backed up first (see reindex._checkpoint_and_backup)."""
    import threading as _threading

    from ingest import DEFAULT_MODEL

    model = str((body or {}).get("model") or DEFAULT_MODEL)
    db_path = State.db_path

    def _worker() -> None:
        try:
            from reindex import _checkpoint_and_backup, reindex_embeddings
            from store import connect as _connect

            _checkpoint_and_backup(db_path)
            conn = _connect(db_path)
            try:
                res = reindex_embeddings(conn, model_name=model)
                conn.commit()
                log.info("admin reindex complete: %s", res)
            finally:
                conn.close()
        except Exception:
            log.exception("admin reindex failed")

    _threading.Thread(target=_worker, name="minion-reindex", daemon=True).start()
    return {"started": True, "model": model, "note": "restart recommended after completion"}


@app.get("/graph/scaffold")
def graph_scaffold(profile_id: Optional[str] = None) -> Dict[str, Any]:
    conn = State.conn()
    from store import graph_scaffold_list

    out = graph_scaffold_list(conn, profile_id=profile_id)
    try:
        from graph_fill import pick_next_gap

        out["has_fill_gap"] = pick_next_gap(conn, State.data_dir) is not None
        from librarian import stream_state

        fs = stream_state(conn, State.data_dir)
        out["librarian"] = {
            "active_thread_id": fs.get("active_thread_id"),
            "needs_question": fs.get("needs_question"),
            "question_preview": (fs.get("question_body_md") or "")[:160],
            "has_gap": fs.get("has_gap"),
        }
    except Exception:
        out["has_fill_gap"] = False
        out["librarian"] = None
    try:
        from graph_ambient import build_graph_spine

        spine = build_graph_spine(conn, State.data_dir, max_active=6)
        out["spine"] = {
            "active_nodes": spine.get("active_nodes") or [],
            "spine_md": spine.get("spine_md") or "",
        }
    except Exception:
        out["spine"] = {"active_nodes": [], "spine_md": ""}
    return out


@app.get("/graph/context")
def graph_context(subject: str = "") -> Dict[str, Any]:
    return build_graph_context(State.conn(), State.data_dir, subject=subject)


@app.get("/context/bundle")
def context_bundle_route(subject: str = "", for_mcp: bool = False) -> Dict[str, Any]:
    from context_core import context_bundle

    return context_bundle(State.conn(), State.data_dir, subject=subject, for_mcp=for_mcp)


@app.get("/context/platform")
def context_platform_meta() -> Dict[str, Any]:
    from context_platform import CONTEXT_BUNDLE_SCHEMA_VERSION
    from consent_policy import privacy_matrix

    return {
        "schema_version": CONTEXT_BUNDLE_SCHEMA_VERSION,
        "layers": ["vault", "context_server", "world_model", "live_preferences"],
        "doc": "docs/CONTEXT_PLATFORM.md",
        "privacy_matrix": privacy_matrix(),
    }


@app.get("/privacy/matrix")
def privacy_matrix_route() -> Dict[str, Any]:
    from consent_policy import privacy_matrix

    return privacy_matrix()


@app.get("/graph/candidates")
def graph_candidates(status: str = "open", limit: int = 50) -> Dict[str, Any]:
    rows = graph_candidate_list(State.conn(), status=status, limit=limit)
    return {"candidates": rows, "count": len(rows)}


@app.get("/graphify/status")
def graphify_status() -> Dict[str, Any]:
    from graphify_adapter import status as graphify_status_fn

    return graphify_status_fn(State.data_dir)


@app.post("/graphify/shadow-build")
def graphify_shadow_build() -> Dict[str, Any]:
    from graphify_adapter import run_graphify_shadow

    conn = State.conn()
    result = run_graphify_shadow(conn, State.data_dir)
    conn.commit()
    return result


@app.post("/graphify/reconcile")
def graphify_reconcile() -> Dict[str, Any]:
    from graphify_adapter import reconcile_graph_truth

    conn = State.conn()
    result = reconcile_graph_truth(conn, State.data_dir)
    conn.commit()
    return result


@app.post("/graph/candidates/{candidate_id}/resolve")
def graph_candidate_resolve_route(
    candidate_id: str, body: GraphCandidateResolveBody
) -> Dict[str, Any]:
    conn = State.conn()
    if graph_candidate_get(conn, candidate_id) is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    result = apply_graph_candidate_resolution(
        conn,
        candidate_id,
        status=body.status,
        payload=body.payload,
        data_dir=State.data_dir,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "candidate resolve failed")
    conn.commit()
    return {"candidate": result.get("candidate"), "result": result}


@app.post("/graph/extract")
def graph_extract_route(body: GraphExtractBody) -> Dict[str, Any]:
    """L3: extract entities from recent (or given) sources into confirmable candidates."""
    from corpus_extract import extract_entities_for_sources

    conn = State.conn()
    result = extract_entities_for_sources(
        conn,
        State.data_dir,
        body.source_ids or None,
    )
    conn.commit()
    return result


@app.post("/graph/reshuffle")
def graph_reshuffle_route(body: GraphReshuffleBody) -> Dict[str, Any]:
    """The Librarian decides whether to update the graph now, and does the work if so."""
    from corpus_extract import reconsider_graph

    conn = State.conn()
    result = reconsider_graph(conn, State.data_dir, force=bool(body.force))
    conn.commit()
    return result


@app.get("/menu/status")
def menu_status() -> Dict[str, Any]:
    return build_menu_status(State.conn(), State.data_dir)


@app.get("/chat/threads")
def chat_threads_list(status: str = "open", limit: int = 30) -> Dict[str, Any]:
    return graph_clarify.list_threads(State.conn(), status=status, limit=limit)


@app.get("/chat/badge")
def chat_badge() -> Dict[str, int]:
    return graph_clarify.badge(State.conn())


@app.get("/chat/threads/{thread_id}")
def chat_thread_get(thread_id: str) -> Dict[str, Any]:
    t = graph_clarify.get_thread(State.conn(), thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="thread not found")
    return t


@app.post("/chat/threads/next")
def chat_threads_next() -> Dict[str, Any]:
    out = graph_clarify.next_clarification(State.conn(), State.data_dir)
    State.conn().commit()
    return out


class ChatReplyBody(BaseModel):
    body: str = ""
    action: Optional[str] = None


@app.post("/chat/threads/{thread_id}/reply")
def chat_thread_reply(thread_id: str, body: ChatReplyBody) -> Dict[str, Any]:
    out = graph_clarify.reply(
        State.conn(),
        thread_id,
        body=body.body,
        action=body.action,
        data_dir=State.data_dir,
    )
    State.conn().commit()
    return out


class LibrarianReplyBody(BaseModel):
    message: str = ""
    thread_id: Optional[str] = None
    action: Optional[str] = None


class OnboardingChatBody(BaseModel):
    step: str = "name"
    display_name: str = ""
    transcript: List[Dict[str, str]] = Field(default_factory=list)
    permission_status: Dict[str, str] = Field(default_factory=dict)


class OnboardingProfileBody(BaseModel):
    display_name: str = ""


class GeminiKeyBody(BaseModel):
    api_key: str


@app.put("/settings/gemini-key")
def put_gemini_key(body: GeminiKeyBody) -> Dict[str, Any]:
    key = (body.api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key required")
    secret_dir = State.data_dir / ".secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    path = secret_dir / "gemini_api_key"
    path.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return {"ok": True, "configured": True}


@app.post("/chat/agent/onboarding")
def chat_agent_onboarding(body: OnboardingChatBody) -> Dict[str, Any]:
    from onboarding_chat import onboarding_reply

    text, used_llm = onboarding_reply(
        step=body.step,
        display_name=body.display_name,
        transcript=body.transcript,
        data_dir=State.data_dir,
        permission_status=body.permission_status,
    )
    return {"message": text, "llm": used_llm}


class SessionOpenBody(BaseModel):
    display_name: str = ""


@app.post("/session/open")
def session_open_route(body: SessionOpenBody = SessionOpenBody()) -> Dict[str, Any]:
    from session_open import open_session

    out = open_session(
        State.conn(),
        State.data_dir,
        display_name=(body.display_name or "").strip(),
    )
    State.conn().commit()
    return out


@app.post("/onboarding/profile")
def onboarding_save_profile(body: OnboardingProfileBody) -> Dict[str, Any]:
    from preference_promotion import record_display_name

    name = (body.display_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="display_name required")
    out = record_display_name(State.conn(), display_name=name, source="onboarding")
    State.conn().commit()
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "profile_save_failed")
    return out


class ResourcePollBody(BaseModel):
    resource_id: str
    uses: bool
    note: str = ""


class ConnectorIntentBody(BaseModel):
    source_text: str = ""
    resource_id: str = ""


@app.get("/onboarding/resource-poll/next")
def onboarding_resource_poll_next() -> Dict[str, Any]:
    from connector_intent import load_resource_poll, next_poll_question

    q = next_poll_question(State.data_dir)
    return {"question": q, "state": load_resource_poll(State.data_dir)}


@app.post("/onboarding/resource-poll")
def onboarding_resource_poll_answer(body: ResourcePollBody) -> Dict[str, Any]:
    from connector_intent import record_poll_answer

    out = record_poll_answer(
        State.conn(),
        State.data_dir,
        resource_id=body.resource_id,
        answer=body.uses,
        free_text=body.note,
    )
    State.conn().commit()
    return out


@app.post("/onboarding/connector-intent")
def onboarding_connector_intent(body: ConnectorIntentBody) -> Dict[str, Any]:
    from connector_intent import create_connector_intent, record_freeform_connector_intent

    conn = State.conn()
    if body.source_text.strip():
        out = record_freeform_connector_intent(conn, State.data_dir, source_text=body.source_text)
    elif body.resource_id.strip():
        out = {
            "ok": True,
            **create_connector_intent(conn, resource_id=body.resource_id.strip(), source="api"),
        }
    else:
        raise HTTPException(status_code=400, detail="source_text or resource_id required")
    conn.commit()
    return out


class E2eSeedGraphGapBody(BaseModel):
    name: str = "E2E Journey Person"


def _e2e_dev_tools_allowed() -> bool:
    if os.environ.get("MINION_E2E") == "1":
        return True
    dd = str(State.data_dir).lower()
    return "pytest-of" in dd or "/pytest-" in dd or "/var/folders/" in dd or "/tmp/" in dd


@app.post("/dev/e2e/seed-graph-gap")
def dev_e2e_seed_graph_gap(body: E2eSeedGraphGapBody) -> Dict[str, Any]:
    """Playwright-only: insert a sparse person and open the next agent thread."""
    if not _e2e_dev_tools_allowed():
        raise HTTPException(status_code=404, detail="not available")
    import librarian
    from chat_store import chat_threads_list
    from store import _new_id

    conn = State.conn()
    for row in chat_threads_list(conn, status="open", limit=50):
        librarian.dismiss(conn, str(row["thread_id"]))
    nid = _new_id("gn")
    now = time.time()
    name = (body.name or "E2E Journey Person").strip()[:120]
    from store import graph_active_profile_id

    profile_id = graph_active_profile_id(conn)
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "profile_id, created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0.5, '[]', 'vault_local', ?, ?, ?)",
        (nid, name, profile_id, now, now),
    )
    from graph_fill import compose_question, open_thread_for_gap, pick_next_gap

    out = librarian.next_question(conn, State.data_dir)
    thread = out.get("thread") or {}
    if not thread.get("thread_id"):
        gap = pick_next_gap(conn, State.data_dir)
        if gap:
            opened = open_thread_for_gap(conn, gap, data_dir=State.data_dir)
            thread = opened.get("thread") or {}
    tid = thread.get("thread_id")
    if tid:
        from chat_store import chat_message_insert, chat_thread_get

        full = chat_thread_get(conn, tid) or {}
        msgs = full.get("messages") or []
        has_q = any(
            str(m.get("body_md") or "").strip() for m in msgs if m.get("role") == "assistant"
        )
        if not has_q:
            gap = (full.get("meta") or {}).get("gap") or {"gap_type": "person", "label": name}
            body = (compose_question(conn, gap, data_dir=State.data_dir) or "").strip()
            if not body:
                body = f"Who is **{name}** to you, and how do you know them?"
            chat_message_insert(
                conn,
                thread_id=tid,
                role="assistant",
                body_md=body,
                meta={"speaker": "Minion", "gap": gap},
            )
    conn.commit()
    return {
        "ok": True,
        "node_id": nid,
        "thread_id": tid,
        "created": bool(out.get("created")),
    }


@app.post("/chat/agent/next")
def chat_librarian_next() -> Dict[str, Any]:
    import librarian

    out = librarian.next_question(State.conn(), State.data_dir)
    State.conn().commit()
    return out


def _resolve_librarian_thread_id(body: LibrarianReplyBody) -> str:
    import librarian

    tid = body.thread_id
    if not tid:
        active = librarian.active_thread(State.conn())
        if not active:
            raise HTTPException(status_code=400, detail="no active thread; call /chat/agent/next")
        tid = active["thread_id"]
    return tid


def _chat_sse_response(event_iter) -> StreamingResponse:
    from chat_sse import sse_line

    def gen():
        tid = ""
        try:
            for payload in event_iter():
                if payload.get("event") == "error":
                    yield sse_line("error", payload.get("data") or {})
                    return
                tid = str((payload.get("data") or {}).get("thread_id") or tid)
                yield sse_line(str(payload.get("event") or "message"), payload.get("data") or {})
        except Exception as exc:
            log.exception("chat stream failed")
            yield sse_line("error", {"code": "stream_failed", "message": str(exc)})
        finally:
            try:
                State.conn().commit()
            except Exception:
                log.exception("chat stream commit failed")
            try:
                from chat_store import chat_open_count

                _schedule_broadcast(
                    {
                        "type": "chat_updated",
                        "thread_id": tid or None,
                        "open_count": chat_open_count(State.conn()),
                    }
                )
            except Exception:
                pass
            yield sse_line("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/chat/agent/reply")
def chat_librarian_reply(body: LibrarianReplyBody) -> Dict[str, Any]:
    import librarian

    tid = _resolve_librarian_thread_id(body)
    out = librarian.reply(
        State.conn(),
        tid,
        body=body.message,
        action=body.action,
        data_dir=State.data_dir,
    )
    State.conn().commit()
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "reply failed")
    try:
        from chat_store import chat_open_count

        _schedule_broadcast(
            {
                "type": "chat_updated",
                "thread_id": tid,
                "open_count": chat_open_count(State.conn()),
            }
        )
    except Exception:
        pass
    return out


@app.post("/chat/agent/reply/stream")
def chat_librarian_reply_stream(body: LibrarianReplyBody) -> StreamingResponse:
    import librarian

    tid = _resolve_librarian_thread_id(body)

    def events():
        yield from librarian.stream_reply(
            State.conn(),
            tid,
            body=body.message,
            action=body.action,
            data_dir=State.data_dir,
        )

    return _chat_sse_response(events)


@app.post("/chat/agent/dismiss")
def chat_librarian_dismiss(body: LibrarianReplyBody) -> Dict[str, Any]:
    import librarian

    if not body.thread_id:
        raise HTTPException(status_code=400, detail="thread_id required")
    out = librarian.dismiss(State.conn(), body.thread_id)
    State.conn().commit()
    return out


@app.get("/keys/capabilities")
def keys_capabilities_list() -> Dict[str, Any]:
    return {"items": keys_api.list_capabilities(State.conn())}


class KeysLinkBody(BaseModel):
    cap_key: str
    vault_ref: str
    label: str
    provider: str = ""


@app.post("/keys/link")
def keys_link(body: KeysLinkBody) -> Dict[str, Any]:
    out = keys_api.link_capability(
        State.conn(),
        cap_key=body.cap_key,
        vault_ref=body.vault_ref,
        label=body.label,
        provider=body.provider,
    )
    State.conn().commit()
    return out


@app.post("/keys/unlink")
def keys_unlink(cap_key: str) -> Dict[str, Any]:
    ok = keys_api.unlink_capability(State.conn(), cap_key=cap_key)
    State.conn().commit()
    return {"ok": ok}


@app.get("/health")
def health_summary() -> Dict[str, Any]:
    conn = State.conn()
    database = _database_status()
    watcher_running = (
        _watcher_thread is not None and _watcher_thread.is_alive()
        if _watcher_thread
        else False
    )
    return {
        "service": "minion-api",
        "product": "minion",
        "version": __version__,
        "status": "ok" if database.get("ok") else "degraded",
        "data_dir": str(State.data_dir),
        "db_path": str(State.db_path),
        "database": database,
        "watcher": {
            "running": watcher_running,
            "mode": _watcher_mode,
        },
        "counts": _counts(),
        "sync_sources": sync_sources_list(conn),
        "open_issues": system_issues_open(conn, limit=20),
    }


@app.post("/health/issues/{issue_id}/resolve")
def health_issue_resolve(issue_id: str) -> Dict[str, Any]:
    ok = system_issue_resolve(State.conn(), issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="issue not found or already resolved")
    State.conn().commit()
    return {"status": "ok", "issue_id": issue_id}


@app.get("/wiki/pages")
def wiki_pages_list(
    page_type: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    rows = wiki_page_list(
        State.conn(), page_type=page_type, status=status, q=q, limit=limit
    )
    return {"pages": rows, "count": len(rows)}


@app.post("/wiki/pages")
def wiki_pages_create(body: WikiPageBody) -> Dict[str, Any]:
    conn = State.conn()
    pid = wiki_page_upsert(
        conn,
        page_id=body.page_id,
        page_type=body.page_type,
        title=body.title,
        body_md=body.body_md,
        status=body.status,
        meta=body.meta,
    )
    conn.commit()
    page = wiki_page_get(conn, pid)
    return {"page": page, "page_id": pid}


@app.get("/wiki/pages/{page_id}")
def wiki_pages_detail(page_id: str) -> Dict[str, Any]:
    page = wiki_page_get(State.conn(), page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    links = wiki_links_for_page(State.conn(), page_id)
    return {"page": page, "links": links}


@app.patch("/wiki/pages/{page_id}")
def wiki_pages_patch(page_id: str, body: WikiPageBody) -> Dict[str, Any]:
    conn = State.conn()
    existing = wiki_page_get(conn, page_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="page not found")
    wiki_page_upsert(
        conn,
        page_id=page_id,
        page_type=body.page_type,
        title=body.title,
        body_md=body.body_md,
        status=body.status,
        meta=body.meta or existing.get("meta"),
    )
    conn.commit()
    return {"page": wiki_page_get(conn, page_id)}


@app.delete("/wiki/pages/{page_id}")
def wiki_pages_delete(page_id: str) -> Dict[str, Any]:
    ok = wiki_page_delete(State.conn(), page_id)
    if not ok:
        raise HTTPException(status_code=404, detail="page not found")
    State.conn().commit()
    return {"status": "ok", "page_id": page_id}


@app.post("/wiki/pages/{page_id}/links")
def wiki_pages_link(page_id: str, body: WikiLinkBody) -> Dict[str, Any]:
    conn = State.conn()
    if wiki_page_get(conn, page_id) is None:
        raise HTTPException(status_code=404, detail="page not found")
    if wiki_page_get(conn, body.to_page_id) is None:
        raise HTTPException(status_code=404, detail="target page not found")
    lid = wiki_link_add(
        conn, from_page_id=page_id, to_page_id=body.to_page_id, link_kind=body.link_kind
    )
    conn.commit()
    return {"link_id": lid, "links": wiki_links_for_page(conn, page_id)}


@app.get("/tasks")
def tasks_list(
    status: Optional[str] = None,
    origin: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    rows = task_list(State.conn(), status=status, origin=origin, limit=limit)
    return {"tasks": rows, "count": len(rows)}


@app.get("/tasks/{task_id}")
def tasks_detail(task_id: str) -> Dict[str, Any]:
    row = task_get(State.conn(), task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    out = None
    if row.get("output_id"):
        out = output_get(State.conn(), row["output_id"])
    return {"task": row, "output": out}


@app.post("/tasks/infer")
def tasks_infer(body: TaskInferBody) -> Dict[str, Any]:
    conn = State.conn()
    tid = task_infer_insert(
        conn,
        title=body.title,
        body_md=body.body_md,
        origin=body.origin,
        priority=body.priority,
        context_refs=body.context_refs,
        wiki_refs=body.wiki_refs,
    )
    conn.commit()
    return {"task": task_get(conn, tid), "task_id": tid}


@app.patch("/tasks/{task_id}")
def tasks_patch(task_id: str, body: TaskPatchBody) -> Dict[str, Any]:
    conn = State.conn()
    row = task_patch(
        conn, task_id, status=body.status, title=body.title, body_md=body.body_md
    )
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    conn.commit()
    return {"task": row}


@app.post("/tasks/{task_id}/outputs")
def tasks_output_create(task_id: str, body: OutputBody) -> Dict[str, Any]:
    conn = State.conn()
    if task_get(conn, task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    oid = output_create(
        conn,
        task_id=task_id,
        kind=body.kind,
        body_md=body.body_md,
        wiki_read=body.wiki_read,
    )
    conn.commit()
    return {"output": output_get(conn, oid), "output_id": oid}


@app.get("/outputs/{output_id}")
def outputs_detail(output_id: str) -> Dict[str, Any]:
    row = output_get(State.conn(), output_id)
    if row is None:
        raise HTTPException(status_code=404, detail="output not found")
    return {"output": row}


@app.patch("/outputs/{output_id}")
def outputs_patch(output_id: str, body: OutputPatchBody) -> Dict[str, Any]:
    conn = State.conn()
    row = output_patch(conn, output_id, status=body.status, body_md=body.body_md)
    if row is None:
        raise HTTPException(status_code=404, detail="output not found")
    conn.commit()
    return {"output": row}


@app.post("/ambient/index-ax")
def ambient_index_ax(body: AmbientIndexAxBody = Body(default_factory=AmbientIndexAxBody)) -> Dict[str, Any]:
    conn = State.conn()
    out = index_ax_from_stream(data_dir=State.data_dir, conn=conn, dry_run=body.dry_run)
    if not body.dry_run:
        conn.commit()
    return out


@app.post("/screen-memory/remember")
def screen_memory_remember(
    body: ScreenRememberBody = Body(default_factory=ScreenRememberBody),
) -> Dict[str, Any]:
    return remember_screen(
        State.conn(),
        State.data_dir,
        max_lines=body.max_lines,
        ingest_screenshots=body.ingest_screenshots,
        run_adapters=body.run_adapters,
    )


@app.get("/screen-memory/search")
def screen_memory_search(
    q: str,
    top_k: int = 8,
    app: str = "",
    after: Optional[float] = None,
    before: Optional[float] = None,
) -> Dict[str, Any]:
    return screen_search(
        State.conn(),
        q,
        top_k=max(1, min(int(top_k), 20)),
        app=app,
        after=after,
        before=before,
    )


@app.get("/screen-memory/summarize-last")
def screen_memory_summarize_last(minutes: int = 30) -> Dict[str, Any]:
    return summarize_last(State.conn(), minutes=minutes)


@app.get("/screen-memory/what-was-i-doing")
def screen_memory_what_was_i_doing(minutes: int = 20) -> Dict[str, Any]:
    return what_was_i_doing(State.conn(), minutes=minutes)


@app.get("/screen-memory/guidance")
def screen_memory_guidance(minutes: int = 30) -> Dict[str, Any]:
    return miyagi_guidance(State.conn(), State.data_dir, minutes=minutes)


@app.get("/screen-memory/status")
def screen_memory_status_route(minutes: int = 60, probe: bool = False) -> Dict[str, Any]:
    return screen_memory_status(State.conn(), State.data_dir, minutes=minutes, run_probe=probe)


@app.get("/screen-memory/events")
def screen_memory_events(minutes: int = 30, limit: int = 80) -> Dict[str, Any]:
    since = time.time() - max(1, min(int(minutes), 24 * 60)) * 60.0
    rows = screen_memory_events_since(
        State.conn(),
        since_ts=since,
        limit=max(1, min(int(limit), 500)),
    )
    return {"events": rows, "count": len(rows)}


@app.post("/screen-memory/create-task")
def screen_memory_create_task(
    body: ScreenCreateTaskBody = Body(default_factory=ScreenCreateTaskBody),
) -> Dict[str, Any]:
    return create_task_from_recent_screen(
        State.conn(),
        minutes=body.minutes,
        title=body.title,
    )


@app.get("/recent-work")
def recent_work_route(days: float = 3.0) -> Dict[str, Any]:
    """Apps by active attention + on-screen work signals over the last `days`."""
    import time as _time

    from attention_rollup import recent_work_digest

    days = max(0.5, min(30.0, days))
    return recent_work_digest(State.conn(), since_ts=_time.time() - days * 86400.0)


@app.get("/maintenance/lifecycle-status")
def maintenance_lifecycle_status() -> Dict[str, Any]:
    """Per-table counts + retention windows + protected (never-deleted) kinds."""
    from memory_lifecycle import lifecycle_status

    return lifecycle_status(State.conn())


@app.post("/maintenance/storage-report")
def maintenance_storage_report() -> Dict[str, Any]:
    conn = State.conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM ambient_events").fetchone()
    amb_count = int(row["n"]) if row else 0
    return {
        "chunk_storage_tiers": chunk_storage_tier_counts(conn),
        "ambient_event_count": amb_count,
        "sqlite": sqlite_storage_fingerprint(conn),
        "note": "Compaction tiers are metadata-first; sqlite.freelist_bytes_approx is reclaimable "
        "only after an offline VACUUM. Cold offload hooks land behind policy.",
    }


@app.post("/maintenance/storage-tier-promote-stale")
def maintenance_storage_tier_promote_stale(body: StorageTierPromoteStaleBody) -> Dict[str, Any]:
    """Promote tiers (hot→warm, warm→cold, …) when ``sources.updated_at`` is older than threshold."""
    try:
        f_t, t_t = validate_stale_tier_promotion(body.from_tier, body.to_tier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    thr = time.time() - float(body.min_source_age_days) * 86400.0
    conn = State.conn()
    kinds = body.source_kinds
    n_cand = count_chunks_stale_source_tier_promotion_candidates(
        conn, source_updated_before=thr, source_kinds=kinds, from_tier=f_t
    )
    if body.dry_run:
        return {
            "dry_run": True,
            "candidates": n_cand,
            "promoted": 0,
            "source_updated_before_unix": thr,
            "min_source_age_days": body.min_source_age_days,
            "source_kinds": kinds,
            "from_tier": f_t,
            "to_tier": t_t,
        }
    promoted = promote_chunks_for_stale_sources(
        conn,
        source_updated_before=thr,
        source_kinds=kinds,
        from_tier=f_t,
        to_tier=t_t,
    )
    identity_audit_log_append(
        conn,
        action="storage_tier_promote_stale",
        detail={
            "promoted": promoted,
            "source_updated_before_unix": thr,
            "min_source_age_days": body.min_source_age_days,
            "source_kinds": kinds,
            "from_tier": f_t,
            "to_tier": t_t,
        },
    )
    conn.commit()
    return {
        "dry_run": False,
        "candidates": n_cand,
        "promoted": promoted,
        "source_updated_before_unix": thr,
        "min_source_age_days": body.min_source_age_days,
        "source_kinds": kinds,
        "from_tier": f_t,
        "to_tier": t_t,
        "chunk_storage_tiers": chunk_storage_tier_counts(conn),
    }


@app.post("/maintenance/storage-tier-consolidate-warm")
def maintenance_storage_tier_consolidate_warm(body: StorageTierConsolidateWarmBody) -> Dict[str, Any]:
    """Consolidate chunks from a source into warm-tier summaries via LLM."""
    conn = State.conn()
    result = consolidate_chunks_to_warm(conn, source_id=body.source_id, data_dir=State.data_dir)
    identity_audit_log_append(
        conn,
        action="storage_tier_consolidate_warm",
        detail={
            "source_id": body.source_id,
            "original_count": result["original_count"],
            "summary_count": result["summary_count"],
            "promoted": result["promoted"],
        },
    )
    conn.commit()
    return {
        "source_id": body.source_id,
        **result,
        "chunk_storage_tiers": chunk_storage_tier_counts(conn),
    }


@app.post("/maintenance/storage-tier-offload-cold")
def maintenance_storage_tier_offload_cold(body: StorageTierOffloadColdBody) -> Dict[str, Any]:
    """Offload warm chunks to cold tier (sparse file storage)."""
    conn = State.conn()
    result = offload_chunks_to_cold(conn, source_id=body.source_id, data_dir=State.data_dir)
    identity_audit_log_append(
        conn,
        action="storage_tier_offload_cold",
        detail={
            "source_id": body.source_id,
            "offloaded": result.get("offloaded", 0),
            "file_path": result.get("file_path", ""),
        },
    )
    conn.commit()
    return {
        "source_id": body.source_id,
        **result,
        "chunk_storage_tiers": chunk_storage_tier_counts(conn),
    }


@app.post("/maintenance/chunk-deduplicate")
def maintenance_chunk_deduplicate(body: ChunkDeduplicateBody) -> Dict[str, Any]:
    """Deduplicate chunks by content fingerprint."""
    conn = State.conn()
    result = deduplicate_chunks_by_fingerprint(
        conn, min_chunk_age_days=body.min_chunk_age_days
    )
    identity_audit_log_append(
        conn,
        action="chunk_deduplicate",
        detail={
            "duplicates_found": result["duplicates_found"],
            "duplicates_removed": result["duplicates_removed"],
            "min_chunk_age_days": body.min_chunk_age_days,
        },
    )
    conn.commit()
    return {
        **result,
        "chunk_storage_tiers": chunk_storage_tier_counts(conn),
    }


@app.post("/maintenance/vacuum")
def maintenance_vacuum() -> Dict[str, Any]:
    """Run SQLite VACUUM to reclaim free space."""
    conn = State.conn()
    result = vacuum_database(conn)
    identity_audit_log_append(
        conn,
        action="vacuum",
        detail={
            "before_bytes": result["before_bytes"],
            "after_bytes": result["after_bytes"],
            "reclaimed_bytes": result["reclaimed_bytes"],
        },
    )
    conn.commit()
    return result


@app.post("/maintenance/run-compaction")
def maintenance_run_compaction() -> Dict[str, Any]:
    """Run all compaction jobs: ambient consolidation, chunk deduplication."""
    from ambient_consolidation import run_ambient_consolidation
    
    conn = State.conn()
    results = {}
    
    # Ambient consolidation
    try:
        ambient_result = run_ambient_consolidation(conn, State.data_dir, force=True)
        results["ambient_consolidation"] = ambient_result
    except Exception as e:
        results["ambient_consolidation"] = {"error": str(e)}
    
    # Chunk deduplication
    try:
        dedup_result = deduplicate_chunks_by_fingerprint(conn, min_chunk_age_days=7.0)
        results["chunk_deduplication"] = dedup_result
    except Exception as e:
        results["chunk_deduplication"] = {"error": str(e)}
    
    identity_audit_log_append(
        conn,
        action="run_compaction",
        detail=results,
    )
    conn.commit()
    
    return {
        "results": results,
        "chunk_storage_tiers": chunk_storage_tier_counts(conn),
    }


@app.post("/identity/export")
def identity_export(body: IdentityExportBody) -> Dict[str, Any]:
    if body.out_path:
        out = Path(body.out_path).expanduser().resolve()
    else:
        exp = State.data_dir / "exports"
        exp.mkdir(parents=True, exist_ok=True)
        out = exp / f"minion-identity-{int(time.time())}.zip"
    try:
        meta = write_identity_export_zip(
            State.conn(),
            out_path=out,
            data_dir=State.data_dir,
            include_chunk_index=body.include_chunk_index,
            include_voice_files=body.include_voice_files,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")
    return meta


@app.get("/chunks/{chunk_id}")
def chunk_detail(chunk_id: str, max_chars: int = 4000) -> Dict[str, Any]:
    row = get_chunk(State.conn(), chunk_id)
    if row is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    text = row["text"]
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return {
        "chunk_id": row["chunk_id"],
        "source_id": row["source_id"],
        "role": row["role"],
        "path": row["path"],
        "kind": row["kind"],
        "mtime": row["mtime"],
        "text": text,
        "meta": row["meta"],
    }


def _resolve_file_dest(src_path: Path) -> Path:
    """Single-file destination under the inbox with collision-dedupe."""
    dest = State.inbox / src_path.name
    if not dest.exists() or dest.resolve() == src_path:
        return dest
    stem, suf = dest.stem, dest.suffix
    i = 1
    while True:
        candidate = State.inbox / f"{stem} ({i}){suf}"
        if not candidate.exists():
            return candidate
        i += 1


def _resolve_dir_dest(src_dir: Path) -> Path:
    """Directory destination under the inbox with collision-dedupe."""
    dest = State.inbox / src_dir.name
    if not dest.exists():
        return dest
    i = 1
    while True:
        candidate = State.inbox / f"{src_dir.name} ({i})"
        if not candidate.exists():
            return candidate
        i += 1


def _safe_context_filename(title: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in title.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    if not cleaned:
        cleaned = "quick-context"
    return cleaned[:80]


def _copy_tree_into_inbox(src_dir: Path, dest_root: Path) -> List[Path]:
    """Mirror src_dir into dest_root under the inbox, skipping junk dirs.

    Implementation note: we stage the copy into a sibling dir OUTSIDE the
    inbox first, then atomically rename it into place. Without this, the
    watcher's fs-event debouncer can flush on the first copied file - long
    before the rest of the tree arrives - and mis-detect a ChatGPT export
    as a single loose JSON. The rename guarantees the tree materializes
    under the inbox as one consistent burst of events.
    """
    copied: List[Path] = []
    staging_parent = State.data_dir / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix="ingest-", dir=str(staging_parent)))
    stage_target = staging_dir / dest_root.name
    stage_target.mkdir(parents=True, exist_ok=True)

    try:
        stack: List[Path] = [src_dir]
        while stack:
            cur = stack.pop()
            try:
                entries = list(cur.iterdir())
            except OSError:
                continue
            for p in entries:
                if p.name.startswith("."):
                    continue
                rel = p.relative_to(src_dir)
                target = stage_target / rel
                if p.is_dir():
                    if p.name in SKIP_DIR_NAMES:
                        continue
                    target.mkdir(parents=True, exist_ok=True)
                    stack.append(p)
                elif p.is_file():
                    try:
                        shutil.copy2(str(p), str(target))
                    except OSError:
                        log.exception("copy failed: %s", p)

        # Atomic-ish move into the inbox. Same filesystem (data_dir and
        # data_dir/inbox share a parent), so os.rename is a metadata op
        # and the watcher sees the tree appear as one event burst.
        dest_root.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(stage_target), str(dest_root))
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    for root, _, files in os.walk(dest_root):
        for name in files:
            copied.append(Path(root) / name)
    return copied


def _trigger_post_ingest_graph(*, added: bool) -> None:
    """Graph the corpus right after ingest: mark inference pending and kick a
    background mine. Both are gated on Gemini being configured (no-op otherwise),
    so ingest stays fast and graphing happens as part of the same experience."""
    try:
        from librarian_queue import maybe_enqueue_after_ingest

        conn = connect(State.db_path)
        try:
            maybe_enqueue_after_ingest(conn, skipped=not added)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.debug("post-ingest graph enqueue skipped", exc_info=True)
    if not added:
        return
    try:
        from graph_corpus_mine import (
            schedule_background_graph_mine,
            schedule_corpus_graph_build,
        )

        # Corpus-agnostic build of the user's OWN entities (debounced to once per
        # ingest burst) — the primary graph-on-ingest path.
        schedule_corpus_graph_build(State.data_dir)
        # Scaffold drip stays as the ongoing personal-identity top-up.
        schedule_background_graph_mine(State.data_dir)
    except Exception:
        log.debug("post-ingest graph mine schedule skipped", exc_info=True)


@app.post("/ingest")
async def ingest_endpoint(body: IngestBody) -> Dict[str, Any]:
    """Bring a file or directory into the inbox and ingest it. The HTTP call
    returns as soon as the copy is done; ingestion runs in the background and
    streams progress over the /events WebSocket."""
    src_path = Path(body.path).expanduser().resolve()
    if not src_path.exists():
        raise HTTPException(status_code=404, detail=f"path not found: {src_path}")

    # -------- Directory path: recurse, then ingest every file in the tree ----
    if src_path.is_dir():
        copied_temp = False
        if not body.recursive:
            raise HTTPException(status_code=400, detail="path is a directory; set recursive=true")
        # Preserve tree structure under inbox/<dirname>/ so dropping two
        # 'notes' folders doesn't collapse their contents together.
        try:
            src_path.relative_to(State.inbox)
            # Already inside the inbox -- the watcher is already seeing it.
            inbox_root = src_path
        except ValueError:
            inbox_root = _resolve_dir_dest(src_path)
            if body.move:
                shutil.move(str(src_path), str(inbox_root))
            else:
                _copy_tree_into_inbox(src_path, inbox_root)
                copied_temp = bool(body.temporary)
                if body.temporary:
                    try:
                        from file_tracker import register_tracked_path

                        register_tracked_path(
                            State.data_dir,
                            original_path=src_path,
                            staged_path=inbox_root,
                            kind="directory",
                        )
                    except Exception:
                        log.exception("failed to register tracked directory")

        # ChatGPT export directories are a single logical source, not a
        # pile of loose JSONs. Hand the entire tree to the watcher: it
        # detects export dirs in `_find_chatgpt_export_dirs` and ingests
        # them via the chatgpt_export parser in one atomic pass. Running
        # a duplicate dir-ingest from here would race the watcher on the
        # same source_id and blank out the DB on commit-collision.
        if _looks_like_chatgpt_export(inbox_root):
            # Register cancel flag for this path
            with State.cancel_flags_lock:
                State.cancel_flags[str(inbox_root)] = {"cancelled": False}
            
            await _broadcast({
                "type": "ingest_started",
                "path": str(inbox_root),
                "count": 1,
                "kind_hint": "chatgpt-export",
                "note": "watcher will ingest as a single source",
            })
            return {"queued": str(inbox_root), "kind": "chatgpt-export", "file_count": 1}

        files = _iter_files_in_tree(inbox_root)

        async def _run_tree() -> None:
            with State.active_lock:
                State.active = {
                    "root": str(inbox_root),
                    "total": len(files),
                    "done": 0,
                    "added": 0,
                    "skipped": 0,
                }
                snap = dict(State.active)
            await _broadcast({
                "type": "ingest_started",
                "path": str(inbox_root),
                "count": len(files),
                "active": snap,
            })
            loop = asyncio.get_running_loop()

            def _work_one(p: Path) -> Dict[str, Any]:
                conn = connect(State.db_path)
                try:
                    res = ingest_file(conn, p, refresh=body.refresh, profile_id=body.profile_id)
                    return {
                        "path": res.path,
                        "source_id": res.source_id,
                        "kind": res.kind,
                        "parser": res.parser,
                        "chunk_count": res.chunk_count,
                        "skipped": res.skipped,
                        "reason": res.reason,
                    }
                finally:
                    conn.close()

            for i, p in enumerate(files, 1):
                await _broadcast({
                    "type": "ingest_progress",
                    "path": str(p),
                    "index": i,
                    "total": len(files),
                })
                res = await loop.run_in_executor(None, _work_one, p)
                with State.active_lock:
                    State.active["done"] = i
                    if res.get("source_id"):
                        State.active["added"] += 1
                    else:
                        State.active["skipped"] += 1
                    snap = dict(State.active)
                if res.get("source_id"):
                    await _broadcast({
                        "type": "source_updated",
                        "result": res,
                        "counts": _counts(),
                        "active": snap,
                    })
                else:
                    await _broadcast({
                        "type": "ingest_skipped",
                        "result": res,
                        "active": snap,
                    })
            with State.active_lock:
                final = dict(State.active)
                State.active = {"root": None, "total": 0, "done": 0, "added": 0, "skipped": 0}
            await _broadcast({
                "type": "tree_done",
                "root": str(inbox_root),
                "added": final.get("added", 0),
                "skipped": final.get("skipped", 0),
                "counts": _counts(),
            })
            _trigger_post_ingest_graph(added=final.get("added", 0) > 0)
            if copied_temp:
                shutil.rmtree(inbox_root, ignore_errors=True)

        asyncio.create_task(_run_tree())
        return {
            "queued": str(inbox_root),
            "kind": "directory",
            "file_count": len(files),
            "temporary": copied_temp,
            "original_path": str(src_path) if copied_temp else None,
        }

    # -------- Single file path ---------------------------------------------
    if not src_path.is_file():
        raise HTTPException(status_code=400, detail=f"unsupported path type: {src_path}")

    copied_temp = False
    try:
        src_path.relative_to(State.inbox)
        dest = src_path
    except ValueError:
        dest = _resolve_file_dest(src_path)
        if body.move:
            shutil.move(str(src_path), str(dest))
        else:
            shutil.copy2(str(src_path), str(dest))
            copied_temp = bool(body.temporary)
            if body.temporary:
                try:
                    from file_tracker import register_tracked_path

                    register_tracked_path(
                        State.data_dir,
                        original_path=src_path,
                        staged_path=dest,
                        kind="file",
                    )
                except Exception:
                    log.exception("failed to register tracked file")

    async def _run_ingest() -> Dict[str, Any]:
        await _broadcast({"type": "ingest_started", "path": str(dest)})
        loop = asyncio.get_running_loop()

        def _work() -> Dict[str, Any]:
            conn = connect(State.db_path)
            try:
                res = ingest_file(conn, dest, refresh=body.refresh, profile_id=body.profile_id)
                return {
                    "path": res.path,
                    "source_id": res.source_id,
                    "kind": res.kind,
                    "parser": res.parser,
                    "chunk_count": res.chunk_count,
                    "skipped": res.skipped,
                    "reason": res.reason,
                }
            finally:
                conn.close()

        res = await loop.run_in_executor(None, _work)
        await _broadcast(
            {
                "type": "source_updated" if res.get("source_id") else "ingest_skipped",
                "result": res,
                "counts": _counts(),
            }
        )
        _trigger_post_ingest_graph(added=bool(res.get("source_id")))
        if copied_temp:
            try:
                dest.unlink()
            except OSError:
                pass
        return res

    asyncio.create_task(_run_ingest())
    return {
        "queued": str(dest),
        "kind": "file",
        "temporary": copied_temp,
        "original_path": str(src_path) if copied_temp else None,
    }


@app.post("/ingest/validate")
async def validate_export_endpoint(body: ValidateExportBody) -> Dict[str, Any]:
    """Validate a ChatGPT or Claude export structure before ingestion.
    
    Returns validation errors with file paths and line numbers.
    Empty errors list means the export is valid.
    """
    src_path = Path(body.path).expanduser().resolve()
    if not src_path.exists():
        raise HTTPException(status_code=404, detail=f"path not found: {src_path}")

    if not src_path.is_dir():
        raise HTTPException(status_code=400, detail="path must be a directory (export)")

    # Detect export type
    export_type = None
    if _looks_like_chatgpt_export(src_path):
        export_type = "chatgpt-export"
    elif _looks_like_claude_export(src_path):
        export_type = "claude-export"
    elif _looks_like_gemini_export(src_path):
        export_type = "gemini-export"
    elif _looks_like_copilot_export(src_path):
        export_type = "copilot-export"
    else:
        return {
            "valid": False,
            "errors": [{
                "file_path": str(src_path),
                "line_number": None,
                "message": "Not a recognized export (expected conversations*.json for ChatGPT, Claude, Gemini, or Copilot)"
            }],
            "export_type": None
        }

    # Validate based on export type
    if export_type == "chatgpt-export":
        errors = validate_export_structure(src_path)
    elif export_type == "gemini-export":
        # Gemini exports have simpler validation - just check for conversation JSON files
        errors = []
        if not list(src_path.glob("conversations.json")) and not list(src_path.glob("conversation*.json")):
            errors.append({
                "file_path": str(src_path),
                "line_number": None,
                "message": "No conversations.json or conversation*.json found"
            })
    elif export_type == "copilot-export":
        # Copilot exports have simpler validation - just check for conversation JSON files
        errors = []
        if not list(src_path.glob("conversations.json")) and not list(src_path.glob("copilot*.json")) and not list(src_path.glob("conversation*.json")):
            errors.append({
                "file_path": str(src_path),
                "line_number": None,
                "message": "No conversations.json, copilot*.json, or conversation*.json found"
            })
    else:
        # Claude exports have simpler validation - just check for conversations.json
        errors = []
        if not list(src_path.glob("conversations.json")) and not list(src_path.glob("conversations-*.json")):
            errors.append({
                "file_path": str(src_path),
                "line_number": None,
                "message": "No conversations.json or conversations-*.json found"
            })

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "export_type": export_type
    }


@app.post("/ingest/cancel")
async def cancel_ingest_endpoint(body: CancelIngestBody) -> Dict[str, Any]:
    """Cancel an active ingest operation by path."""
    src_path = str(Path(body.path).expanduser().resolve())
    
    with State.cancel_flags_lock:
        if src_path in State.cancel_flags:
            State.cancel_flags[src_path]["cancelled"] = True
            return {"cancelled": True, "path": src_path}
        else:
            return {"cancelled": False, "reason": "No active ingest for this path"}


@app.post("/ingest/text")
async def ingest_text_endpoint(body: IngestTextBody) -> Dict[str, Any]:
    """Save pasted text as a Markdown file in the inbox, then ingest it."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    title = body.title.strip() or "Quick context"
    State.inbox.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = _resolve_file_dest(State.inbox / f"{stamp}-{_safe_context_filename(title)}.md")
    dest.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")

    async def _run_ingest() -> None:
        await _broadcast({"type": "ingest_started", "path": str(dest), "source": "quick-text"})
        loop = asyncio.get_running_loop()

        def _work() -> Dict[str, Any]:
            conn = connect(State.db_path)
            try:
                res = ingest_file(conn, dest, refresh=False)
                return {
                    "path": res.path,
                    "source_id": res.source_id,
                    "kind": res.kind,
                    "parser": res.parser,
                    "chunk_count": res.chunk_count,
                    "skipped": res.skipped,
                    "reason": res.reason,
                }
            finally:
                conn.close()

        res = await loop.run_in_executor(None, _work)
        await _broadcast(
            {
                "type": "source_updated" if res.get("source_id") else "ingest_skipped",
                "result": res,
                "counts": _counts(),
            }
        )

    asyncio.create_task(_run_ingest())
    return {"queued": str(dest), "kind": "text"}


@app.post("/ingest/webhook")
async def ingest_webhook(request: Request) -> Dict[str, Any]:
    """Ingest pre-chunked JSON (Zapier, Slack bridge, custom scripts).

    JSON body: :class:`IngestWebhookBody`. For line-delimited JSON set
    ``Content-Type: application/x-ndjson`` and pass ``?source_key=…``.
    """
    ct = (request.headers.get("content-type") or "").lower()
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="empty body")

    if "ndjson" in ct or "x-ndjson" in ct:
        sk = (request.query_params.get("source_key") or "").strip()
        if not sk:
            raise HTTPException(
                status_code=400,
                detail="NDJSON mode requires ?source_key=your_stable_id",
            )
        rows: List[Dict[str, Any]] = []
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"ndjson: {e}") from e
            if not isinstance(obj, dict):
                raise HTTPException(status_code=400, detail="each NDJSON line must be a JSON object")
            rows.append(obj)
        body = IngestWebhookBody(
            source_key=sk,
            display_name=None,
            kind="external",
            parser="webhook-ndjson",
            chunks=[WebhookChunk.model_validate(c) for c in rows],
        )
    else:
        try:
            body = IngestWebhookBody.model_validate_json(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    conn = State.conn()
    res = ingest_webhook_payload(
        conn,
        State.data_dir,
        source_key=body.source_key,
        display_name=body.display_name,
        kind=body.kind,
        parser=body.parser,
        chunks=[c.model_dump(mode="json") for c in body.chunks],
        force=False,
    )
    res_dict: Dict[str, Any] = {
        "path": res.path,
        "source_id": res.source_id,
        "kind": res.kind,
        "parser": res.parser,
        "chunk_count": res.chunk_count,
        "skipped": res.skipped,
        "reason": res.reason,
    }
    ev = "ingest_skipped" if res.skipped or not res.source_id else "source_updated"
    _schedule_broadcast({"type": ev, "result": res_dict, "counts": _counts()})
    return {"ok": True, **res_dict}


# ---------------------------------------------------------------------------
# Test workflow verification
# ---------------------------------------------------------------------------


class TestWorkflowBody(BaseModel):
    """Request body for POST /test/workflow."""

    query: str = Field(default="test", min_length=1, max_length=200)


@app.post("/test/workflow")
def test_workflow(body: TestWorkflowBody = TestWorkflowBody()) -> Dict[str, Any]:
    """Drop a sample file and verify retrieval.

    End-to-end test:
    1. Create a sample text file in the inbox
    2. Ingest it directly (same path as watcher/CLI)
    3. Perform a search to verify retrieval
    4. Return results with timing info
    """
    import time as _time

    from ingest import ingest_file

    start_time = _time.time()
    query = body.query

    # Create a sample file with unique content
    test_content = f"""Minion Test Workflow Verification
Generated at: {_time.time()}
Test query: {query}

This is a sample document used to verify that the Minion ingest and retrieval pipeline is working correctly.
It contains enough unique text to be searchable via semantic and keyword search.

Key phrases to test retrieval:
- workflow verification
- test document
- ingest pipeline
- semantic search
- keyword search
"""
    test_filename = f"minion_test_{int(_time.time())}.txt"
    test_path = State.inbox / test_filename

    try:
        test_path.write_text(test_content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to create test file: {e}")

    ingest_start = _time.time()
    try:
        conn = State.conn()
        result = ingest_file(conn, test_path)
        conn.commit()
    except Exception as e:
        try:
            test_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")

    ingestion_time = _time.time() - ingest_start
    source_id = result.source_id
    if not source_id or result.skipped:
        try:
            test_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"Ingestion did not index test file: {result.reason or 'unknown'}",
        )

    # Verify retrieval via search
    try:
        conn = State.conn()
        from store import profile_get_active

        profile_id = profile_get_active(conn)
        model = _get_query_model()
        from ingest import apply_query_prefix

        text = apply_query_prefix(_query_model_name or "", query)
        vec = np.asarray(next(iter(model.embed([text]))), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm

        hits = store_search(conn, vec, top_k=5, profile_id=profile_id)
        found = any(h.source_id == source_id for h in hits)

        # Also try keyword search
        keyword_hits = store_keyword_search(conn, query, top_k=5, profile_id=profile_id)
        keyword_found = any(h.source_id == source_id for h in keyword_hits)

        total_time = _time.time() - start_time

        return {
            "ok": True,
            "test_file": str(test_path),
            "source_id": source_id,
            "ingestion_time": round(ingestion_time, 2),
            "total_time": round(total_time, 2),
            "semantic_search_found": found,
            "keyword_search_found": keyword_found,
            "semantic_hits": len(hits),
            "keyword_hits": len(keyword_hits),
            "query": query,
            "status": "passed" if (found or keyword_found) else "failed",
        }
    except Exception as e:
        log.exception("test workflow search failed")
        raise HTTPException(status_code=500, detail=f"Search verification failed: {e}")
    finally:
        # Clean up the test file and source
        try:
            if source_id:
                delete_source(State.conn(), source_id)
            test_path.unlink(missing_ok=True)
        except Exception:
            log.exception("test cleanup failed")


# ---------------------------------------------------------------------------
# Claude Desktop MCP registration
#
# Two entry points share the same upserter:
#   1. /connect/claude-desktop       — UI "Connect" button; creates config if
#                                      missing (explicit user opt-in).
#   2. _refresh_mcp_on_launch()      — called from lifespan startup; only
#                                      updates an existing entry so we never
#                                      auto-install for users who don't run
#                                      Claude.
#
# We stash a short content hash of the MCP-relevant sources under
# env.MINION_BUILD_SHA. Claude Desktop watches claude_desktop_config.json and
# reconnects any server whose entry mutates, so a hash bump forces it to
# re-read tools/list and initialize.instructions — exactly what "uninstall +
# reinstall" would do, minus the race window where the server goes missing.
# ---------------------------------------------------------------------------


def _refresh_mcp_on_launch() -> None:
    """Called from lifespan startup. Refresh Minion MCP entries when configs
    already exist — never auto-create one. Silent on any failure."""
    if os.environ.get("MINION_SKIP_MCP_REFRESH"):
        return
    try:
        from connector_base import ConnectorRegistry
    except Exception:
        log.exception("mcp: connector registry unavailable for auto-refresh")
        return
    for connector in ConnectorRegistry.list_all().values():
        result = connector.refresh_if_configured("minion")
        if result and result.get("action") in ("created", "refreshed"):
            log.info(
                "mcp: %s %s (sha=%s) — %s will reconnect",
                result["action"],
                result.get("config_path"),
                result.get("build_sha"),
                connector.display_name,
            )


@app.get("/connect/claude-desktop/status")
def connect_claude_desktop_status() -> Dict[str, Any]:
    """Whether Claude Desktop is installed and Minion is registered in its MCP config."""
    from connector_base import ConnectorRegistry

    connector = ConnectorRegistry.get("claude-desktop")
    if connector is None:
        raise HTTPException(status_code=404, detail="Claude Desktop connector not registered")
    status = connector.get_status()
    cfg_path = connector.get_config_path()
    return {
        **status,
        "config_path": str(cfg_path) if cfg_path else None,
    }


@app.get("/connect/cursor/status")
def connect_cursor_status() -> Dict[str, Any]:
    """Whether Cursor is installed and Minion is registered in its MCP config."""
    from connector_base import ConnectorRegistry

    connector = ConnectorRegistry.get("cursor")
    if connector is None:
        raise HTTPException(status_code=404, detail="Cursor connector not registered")
    status = connector.get_status()
    cfg_path = connector.get_config_path()
    return {
        **status,
        "config_path": str(cfg_path) if cfg_path else None,
    }


@app.post("/connect/claude-desktop")
def connect_claude_desktop(body: ConnectBody) -> Dict[str, Any]:
    """Merge the Minion MCP entry into Claude Desktop's config."""
    from connector_base import ConnectorRegistry

    connector = ConnectorRegistry.get("claude-desktop")
    if connector is None:
        raise HTTPException(status_code=404, detail="Claude Desktop connector not registered")
    try:
        return connector.connect(
            server_name=body.server_name,
            config_path_override=body.config_path,
            create_if_missing=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        detail = f"cannot write config: {e.strerror or 'os error'}"
        raise HTTPException(status_code=403, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"connect failed: {e.__class__.__name__}: {e}")


@app.post("/connect/cursor")
def connect_cursor(body: ConnectBody) -> Dict[str, Any]:
    """Merge the Minion MCP entry into Cursor's config."""
    from connector_base import ConnectorRegistry

    connector = ConnectorRegistry.get("cursor")
    if connector is None:
        raise HTTPException(status_code=404, detail="Cursor connector not registered")
    try:
        return connector.connect(
            server_name=body.server_name,
            config_path_override=body.config_path,
            create_if_missing=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        detail = f"cannot write config: {e.strerror or 'os error'}"
        raise HTTPException(status_code=403, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"connect failed: {e.__class__.__name__}: {e}")


# ---------------------------------------------------------------------------
# Generic connector endpoints (using connector abstraction)
# ---------------------------------------------------------------------------


@app.get("/connectors")
def list_connectors() -> Dict[str, Any]:
    """List all available AI assistant connectors with their status."""
    from connector_base import ConnectorRegistry

    return {
        "connectors": ConnectorRegistry.list_available(),
    }


@app.get("/connectors/{connector_id}/status")
def get_connector_status(connector_id: str) -> Dict[str, Any]:
    """Get connection status for a specific connector."""
    from connector_base import ConnectorRegistry

    connector = ConnectorRegistry.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_id}' not found")

    return {
        "connector_id": connector.connector_id,
        "display_name": connector.display_name,
        **connector.get_status(),
    }


@app.post("/connectors/{connector_id}/connect")
def connect_generic(connector_id: str, body: ConnectBody) -> Dict[str, Any]:
    """Connect Minion to a specific AI assistant via its MCP config.

    Generic endpoint that works with any registered connector.
    """
    from connector_base import ConnectorRegistry

    connector = ConnectorRegistry.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_id}' not found")

    try:
        return connector.connect(
            server_name=body.server_name,
            config_path_override=body.config_path,
            create_if_missing=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"connect failed: {e.__class__.__name__}: {e}")


# ---------------------------------------------------------------------------
# Export scheduler endpoints
# ---------------------------------------------------------------------------


class ExportTriggerBody(BaseModel):
    export_path: Optional[str] = None


def _normalize_export_trigger_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Shape trigger responses for the desktop client."""
    if result.get("status") == "error":
        return {
            "ok": False,
            "ingested": 0,
            "message": result.get("error") or "Export trigger failed",
            **result,
        }
    if "success" in result:
        ingested = 1 if result.get("success") else 0
        msg = "Export ingested" if ingested else (result.get("reason") or "Export skipped")
        return {"ok": bool(result.get("success")), "ingested": ingested, "message": msg, **result}
    ingested = int(result.get("successful", 0) or 0)
    status = result.get("status", "completed")
    if status == "no_new_exports":
        message = "No new exports found"
    elif status == "disabled":
        message = "Export scheduler is disabled"
    else:
        message = f"Ingested {ingested} export(s)"
    return {"ok": True, "ingested": ingested, "message": message, **result}


@app.get("/exports/status")
def exports_status() -> Dict[str, Any]:
    """Get export scheduler status and configuration."""
    from export_scheduler import export_interval_sec, export_profile_id, export_scheduler_stats, export_watch_path
    from store import profile_get_active

    conn = State.conn()
    watch_path = export_watch_path(State.data_dir)
    interval = export_interval_sec(State.data_dir)
    stats = export_scheduler_stats(conn)
    resolved_profile = export_profile_id(State.data_dir, conn)

    return {
        "enabled": os.environ.get("MINION_DISABLE_EXPORT_SCHEDULER", "").strip().lower() not in (
            "1",
            "true",
            "yes",
        ),
        "watch_path": str(watch_path) if watch_path else None,
        "watch_path_exists": watch_path.exists() if watch_path else False,
        "interval_sec": interval,
        "interval_hours": interval / 3600.0,
        "export_profile_id": resolved_profile,
        "active_profile_id": profile_get_active(conn),
        **stats,
    }


@app.post("/exports/trigger")
def trigger_export(
    body: ExportTriggerBody = Body(default_factory=ExportTriggerBody),
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Manually trigger export ingestion for a specific file or all new exports."""
    from export_scheduler import trigger_manual_export

    export_path = path or body.export_path
    result = trigger_manual_export(
        State.data_dir,
        State.conn,
        export_path=export_path,
    )
    return _normalize_export_trigger_result(result)


class ExportConfigBody(BaseModel):
    watch_path: Optional[str] = None
    interval_sec: Optional[float] = None
    export_watch_path: Optional[str] = None
    export_interval_sec: Optional[float] = None
    export_profile_id: Optional[str] = None
    enabled: Optional[bool] = None


@app.post("/exports/config")
def configure_exports(body: ExportConfigBody) -> Dict[str, Any]:
    """Configure export scheduler settings."""
    from settings import load_settings, save_settings

    settings = load_settings(State.data_dir)
    resolved_watch = body.watch_path if body.watch_path is not None else body.export_watch_path
    resolved_interval = body.interval_sec if body.interval_sec is not None else body.export_interval_sec

    if resolved_watch is not None:
        settings["export_watch_path"] = resolved_watch

    if resolved_interval is not None:
        settings["export_interval_sec"] = max(300.0, float(resolved_interval))

    if body.export_profile_id is not None:
        pid = body.export_profile_id.strip()
        if pid:
            settings["export_profile_id"] = pid
        else:
            settings.pop("export_profile_id", None)
    
    if body.enabled is not None:
        if body.enabled:
            os.environ.pop("MINION_DISABLE_EXPORT_SCHEDULER", None)
        else:
            os.environ["MINION_DISABLE_EXPORT_SCHEDULER"] = "1"
    
    save_settings(State.data_dir, settings)
    
    return {
        "ok": True,
        "settings": {
            "export_watch_path": settings.get("export_watch_path"),
            "export_interval_sec": settings.get("export_interval_sec"),
            "export_profile_id": settings.get("export_profile_id"),
        },
    }


# ---------------------------------------------------------------------------
# Profile management endpoints
# ---------------------------------------------------------------------------


class ProfileCreateBody(BaseModel):
    profile_id: str
    name: str
    kind: str = "custom"
    is_default: bool = False


class ProfileUpdateBody(BaseModel):
    name: Optional[str] = None
    is_default: Optional[bool] = None


class ProfileSetActiveBody(BaseModel):
    profile_id: str


@app.get("/profiles")
def list_profiles() -> Dict[str, Any]:
    """List all profiles."""
    from store import profile_list

    conn = State.conn()
    profiles = profile_list(conn)
    return {
        "profiles": [
            {
                "profile_id": p.profile_id,
                "name": p.name,
                "kind": p.kind,
                "is_default": p.is_default,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            }
            for p in profiles
        ]
    }


@app.post("/profiles")
def create_profile(body: ProfileCreateBody) -> Dict[str, Any]:
    """Create a new profile."""
    from store import profile_create

    profile = profile_create(
        State.conn(),
        profile_id=body.profile_id,
        name=body.name,
        kind=body.kind,
        is_default=body.is_default,
    )
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "kind": profile.kind,
        "is_default": profile.is_default,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


@app.get("/profiles/active")
def get_active_profile() -> Dict[str, Any]:
    """Get the active profile."""
    from store import profile_get, profile_get_active, profile_set_active

    conn = State.conn()
    active_id = profile_get_active(conn)
    if not active_id:
        from store import profile_ensure_default

        default = profile_ensure_default(conn)
        profile_set_active(conn, default.profile_id)
        active_id = default.profile_id
    profile = profile_get(conn, active_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Active profile not found")
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "kind": profile.kind,
        "is_default": profile.is_default,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


@app.put("/profiles/active")
def set_active_profile(body: ProfileSetActiveBody) -> Dict[str, Any]:
    """Set the active profile."""
    from store import profile_get, profile_set_active

    conn = State.conn()
    profile = profile_get(conn, body.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    profile_set_active(conn, body.profile_id)
    return {"ok": True, "profile_id": body.profile_id}


@app.get("/profiles/{profile_id}/summary")
def get_profile_summary(profile_id: str) -> Dict[str, Any]:
    """Profile card: counts, MCP consent preview, last ingest timestamp."""
    from store import (
        count_chunks,
        count_sources,
        profile_get,
        profile_get_active,
    )

    conn = State.conn()
    profile = profile_get(conn, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    row = conn.execute(
        "SELECT MAX(updated_at) AS ts FROM sources WHERE COALESCE(profile_id, 'default') = ?",
        (profile_id,),
    ).fetchone()
    last_ingest_at: Optional[float] = None
    if row and row["ts"] is not None:
        last_ingest_at = float(row["ts"])

    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "kind": profile.kind,
        "is_default": profile.is_default,
        "is_active": profile_get_active(conn) == profile_id,
        "counts": {
            "sources": count_sources(conn, profile_id=profile_id),
            "chunks": count_chunks(conn, profile_id=profile_id),
        },
        "consent_preview": consent_policy.consent_preview_for_profile(
            State.data_dir, profile_id
        ),
        "last_ingest_at": last_ingest_at,
    }


@app.get("/profiles/{profile_id}")
def get_profile(profile_id: str) -> Dict[str, Any]:
    """Get a specific profile."""
    from store import profile_get

    profile = profile_get(State.conn(), profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "kind": profile.kind,
        "is_default": profile.is_default,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


@app.put("/profiles/{profile_id}")
def update_profile(profile_id: str, body: ProfileUpdateBody) -> Dict[str, Any]:
    """Update a profile."""
    from store import profile_update

    profile = profile_update(
        State.conn(),
        profile_id=profile_id,
        name=body.name,
        is_default=body.is_default,
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "kind": profile.kind,
        "is_default": profile.is_default,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


@app.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> Dict[str, Any]:
    """Delete a profile and associated data."""
    from store import profile_delete

    try:
        profile_delete(State.conn(), profile_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()
    async with State.subscribers_lock:
        State.subscribers.add(ws)
    # Send a snapshot on connect so the UI hydrates without a separate fetch.
    try:
        await ws.send_json({
            "type": "snapshot",
            "counts": _counts(),
            "active": _public_active(),
        })
        while True:
            # We don't expect client messages; drain to keep the connection alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with State.subscribers_lock:
            State.subscribers.discard(ws)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=os.environ.get("MINION_API_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MINION_API_PORT", "8765")))
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    # Default: log to stderr. If MINION_LOG_FILE is set (desktop app), also
    # write to a rotating file so users can debug first-launch issues.
    log_path = os.environ.get("MINION_LOG_FILE", "").strip()
    file_audit = bool(log_path)
    stream_h = logging.StreamHandler()
    if args.verbose:
        stream_h.setLevel(logging.INFO)
    else:
        stream_h.setLevel(logging.WARNING)
    handlers: List[logging.Handler] = [stream_h]
    if log_path:
        try:
            from logging.handlers import RotatingFileHandler

            Path(log_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            file_h = RotatingFileHandler(
                filename=str(Path(log_path).expanduser()),
                maxBytes=10 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            file_h.setLevel(logging.INFO)
            handlers.append(file_h)
        except Exception:
            # Never crash startup due to logging.
            pass

    # Root must allow INFO when a file handler needs it, even if stderr stays WARNING-only.
    root_level = logging.INFO if (args.verbose or file_audit) else logging.WARNING
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

    import uvicorn

    # Tauri's sidecar looks at stdout for readiness; print a single line so
    # the Rust shell can flip from "starting" to "connected".
    print(f"[minion-api] listening http://{args.host}:{args.port}", flush=True)
    if file_audit:
        log.info("listening http://%s:%s (file log=%s)", args.host, args.port, log_path)
    uvicorn_log_level = "info" if (args.verbose or file_audit) else "warning"
    uvicorn.run(app, host=args.host, port=args.port, log_level=uvicorn_log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
