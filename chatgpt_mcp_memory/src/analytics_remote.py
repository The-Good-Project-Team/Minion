"""Opt-out HTTP analytics — anonymous aggregates only.

Telemetry is **on by default**. Users disable it with ``telemetry_opt_out: true``
in ``<data_dir>/settings.json`` (Settings → Support).

The sidecar posts to ``MINION_ANALYTICS_URL`` when set; otherwise the **bundled
default URL** (must stay in sync with ``DEFAULT_MINION_ANALYTICS_URL`` in
``desktop/src-tauri/src/lib.rs``). Set ``MINION_DISABLE_REMOTE_ANALYTICS=1`` on
the host to ship a build that never sets the URL.

The JSON body never includes search queries, file paths, chunk text, or tokens.
Your server still receives normal HTTP metadata (IP, User-Agent, TLS timing) the
same way any website does; disclose that in your privacy policy.

Never raises into callers; failures are dropped on the floor.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from settings import load_settings
from telemetry import data_dir as telemetry_data_dir
from version import __version__

log = logging.getLogger("minion.analytics_remote")

# Sync with desktop/src-tauri/src/lib.rs DEFAULT_MINION_ANALYTICS_URL.
_BUNDLED_ANALYTICS_URL = "https://minion-telemetry.reify.workers.dev/v1/collect"

_SCHEMA = 1
_session_sent = False
_hour_bucket: int = 0
_hour_count: int = 0
_lock = threading.Lock()


def effective_analytics_url() -> str:
    """Resolved POST target (may be empty when remote analytics are fully disabled)."""
    if os.environ.get("MINION_DISABLE_REMOTE_ANALYTICS", "").strip() == "1":
        return ""
    raw = os.environ.get("MINION_ANALYTICS_URL", "").strip()
    if raw:
        return raw
    return _BUNDLED_ANALYTICS_URL


def _install_id(root: Path) -> str:
    p = root / ".minion_install_id"
    try:
        if p.exists():
            s = p.read_text(encoding="utf-8").strip()
            if len(s) >= 8:
                return s
    except OSError:
        pass
    nid = str(uuid.uuid4())
    try:
        p.write_text(nid + "\n", encoding="utf-8")
    except OSError:
        pass
    return nid


def _remote_enabled(root: Path) -> tuple[bool, str]:
    url = effective_analytics_url().strip()
    if not url:
        return False, ""
    try:
        if load_settings(root).get("telemetry_opt_out"):
            return False, ""
    except Exception:
        return False, ""
    return True, url


def _monitoring_enabled(root: Path) -> tuple[bool, str]:
    """Error/crash forwarding is **opt-in** (settings.remote_monitoring=true),
    independent of the opt-out aggregate telemetry above. Same collector URL."""
    url = effective_analytics_url().strip()
    if not url:
        return False, ""
    try:
        if not load_settings(root).get("remote_monitoring"):
            return False, ""
    except Exception:
        return False, ""
    return True, url


def _under_hourly_cap(max_per_hour: int = 120) -> bool:
    global _hour_bucket, _hour_count
    with _lock:
        b = int(time.time() // 3600)
        if b != _hour_bucket:
            _hour_bucket = b
            _hour_count = 0
        if _hour_count >= max_per_hour:
            return False
        _hour_count += 1
        return True


def _post(url: str, body: Dict[str, Any]) -> None:
    def _run() -> None:
        try:
            import urllib.error
            import urllib.request

            data = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": f"Minion/{__version__} ({platform.system()}; {platform.machine()})",
                },
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                resp.read(256)
        except Exception:
            log.debug("analytics POST failed", exc_info=True)

    threading.Thread(target=_run, name="minion-analytics", daemon=True).start()


def emit_session_if_ready() -> None:
    """Fire once per sidecar process when telemetry is allowed + URL is set."""
    global _session_sent
    root = telemetry_data_dir()
    if root is None:
        return
    ok, url = _remote_enabled(root)
    if not ok or not url:
        return
    with _lock:
        if _session_sent:
            return
        _session_sent = True
    body = {
        "schema": _SCHEMA,
        "event": "session",
        "install_id": _install_id(root),
        "app_version": __version__,
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
    _post(url, body)


def _sanitize(kind: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if kind == "search":
        hk = fields.get("hit_kinds")
        if isinstance(hk, list):
            kinds: List[str] = [str(x) for x in hk[:24] if x is not None]
        else:
            kinds = []
        return {
            "schema": _SCHEMA,
            "event": "search",
            "returned": fields.get("returned"),
            "top_k": fields.get("top_k"),
            "mode": fields.get("mode"),
            "rerank": fields.get("rerank"),
            "hit_kinds": kinds,
            "has_kind_filter": bool(fields.get("kind_filter")),
            "has_path_glob": bool(fields.get("path_glob")),
            "has_role_filter": bool(fields.get("role")),
            "has_query": bool(fields.get("query")),
        }
    if kind == "ingest":
        reason = fields.get("reason")
        rcls: Optional[str] = None
        if isinstance(reason, str) and reason.strip():
            rcls = reason.split(":", 1)[0].strip()[:72]
            if any(sep in rcls for sep in ("/", "\\", "..")):
                rcls = "redacted_path_token"
        skipped = bool(fields.get("skipped"))
        return {
            "schema": _SCHEMA,
            "event": "ingest",
            "file_kind": fields.get("file_kind"),
            "parser": fields.get("parser"),
            "chunks": fields.get("chunks"),
            "skipped": skipped,
            "result": fields.get("result"),
            "reason_class": rcls,
        }
    return None


def _scrub(text: str, max_len: int = 600) -> str:
    """Best-effort redaction: drop home-dir prefixes so paths don't leak a
    username, then truncate. The collector still sees error *shapes*, not data."""
    if not isinstance(text, str):
        text = str(text)
    try:
        home = str(Path.home())
        if home and home in text:
            text = text.replace(home, "~")
    except Exception:
        pass
    text = " ".join(text.split())
    return text[:max_len]


def emit_error(
    source: str,
    message: str,
    *,
    detail: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Forward one error/crash report to the collector when monitoring is opted in.

    ``source`` is "sidecar" (API/MCP) or "desktop" (UI). Never raises."""
    try:
        root = telemetry_data_dir()
        if root is None:
            return
        ok, url = _monitoring_enabled(root)
        if not ok or not url:
            return
        if not _under_hourly_cap():
            return
        body: Dict[str, Any] = {
            "schema": _SCHEMA,
            "event": "error",
            "source": str(source)[:24],
            "message": _scrub(message),
            "install_id": _install_id(root),
            "app_version": __version__,
            "os": platform.system(),
            "arch": platform.machine(),
        }
        if detail:
            body["detail"] = _scrub(detail, max_len=2000)
        if isinstance(context, dict) and context:
            # Keep context small + string-only; never trust caller types.
            slim: Dict[str, Any] = {}
            for k, v in list(context.items())[:12]:
                slim[str(k)[:40]] = _scrub(str(v), max_len=200)
            body["context"] = slim
        _post(url, body)
    except Exception:
        log.debug("emit_error failed", exc_info=True)


class _RemoteMonitorHandler(logging.Handler):
    """Logging handler that forwards ERROR+ records to the collector (opt-in).
    Attach once at startup; gating happens per-record inside emit_error."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("minion.analytics"):
                return  # never recurse on our own POST failures
            msg = record.getMessage()
            detail = None
            if record.exc_info:
                import traceback

                detail = "".join(traceback.format_exception(*record.exc_info))
            emit_error(
                "sidecar",
                f"{record.name}: {msg}",
                detail=detail,
                context={"level": record.levelname, "logger": record.name},
            )
        except Exception:
            pass


_monitor_handler_installed = False


def install_log_monitor(level: int = logging.ERROR) -> None:
    """Idempotently attach the remote monitor to the root logger."""
    global _monitor_handler_installed
    with _lock:
        if _monitor_handler_installed:
            return
        _monitor_handler_installed = True
    h = _RemoteMonitorHandler(level=level)
    logging.getLogger().addHandler(h)


def on_telemetry_logged(kind: str, fields: Dict[str, Any]) -> None:
    """Hook from ``telemetry.log_event`` after the local JSONL line is written."""
    root = telemetry_data_dir()
    if root is None:
        return
    ok, url = _remote_enabled(root)
    if not ok or not url:
        return
    if kind not in ("search", "ingest"):
        return
    if not _under_hourly_cap():
        return
    body = _sanitize(kind, fields)
    if body is None:
        return
    body["install_id"] = _install_id(root)
    body["app_version"] = __version__
    body["os"] = platform.system()
    _post(url, body)
