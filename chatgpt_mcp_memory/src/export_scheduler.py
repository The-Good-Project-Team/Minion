"""Export scheduler: monitors folder for new AI assistant exports and auto-ingests them."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SEC = 3600.0  # Check every hour by default
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

_META_LAST_CHECK = "export_scheduler_last_check_at"
_META_LAST_INGESTED = "export_scheduler_last_ingested_count"
_META_TOTAL_INGESTED = "export_scheduler_total_ingested"


def export_interval_sec(data_dir: Optional[Path] = None) -> float:
    """Get export check interval from settings or environment."""
    raw = os.environ.get("MINION_EXPORT_INTERVAL_SEC", "").strip()
    if raw:
        try:
            return max(300.0, float(raw))  # Minimum 5 minutes
        except ValueError:
            pass
    if data_dir:
        try:
            from settings import load_settings

            v = load_settings(Path(data_dir)).get("export_interval_sec")
            if v is not None:
                return max(300.0, float(v))
        except Exception:
            pass
    return _DEFAULT_INTERVAL_SEC


def export_watch_path(data_dir: Optional[Path] = None) -> Optional[Path]:
    """Get the folder path to monitor for new exports."""
    raw = os.environ.get("MINION_EXPORT_WATCH_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    if data_dir:
        try:
            from settings import load_settings

            v = load_settings(Path(data_dir)).get("export_watch_path")
            if v:
                return Path(v).expanduser().resolve()
        except Exception:
            pass
    # Default: watch inbox/exports subdirectory
    if data_dir:
        return Path(data_dir) / "inbox" / "exports"
    return None


def _meta_get(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (key, value),
    )


def export_scheduler_stats(conn) -> dict:
    """Return persisted scheduler counters for status API."""
    last_check_raw = _meta_get(conn, _META_LAST_CHECK)
    last_ingested_raw = _meta_get(conn, _META_LAST_INGESTED)
    total_raw = _meta_get(conn, _META_TOTAL_INGESTED)
    return {
        "last_check_at": float(last_check_raw) if last_check_raw else None,
        "last_ingested_count": int(last_ingested_raw) if last_ingested_raw else 0,
        "total_ingested": int(total_raw) if total_raw else 0,
    }


def _record_tick_stats(conn, ingested_count: int) -> None:
    try:
        now = time.time()
        stats = export_scheduler_stats(conn)
        total = stats["total_ingested"] + max(0, ingested_count)
        _meta_set(conn, _META_LAST_CHECK, str(now))
        _meta_set(conn, _META_LAST_INGESTED, str(max(0, ingested_count)))
        _meta_set(conn, _META_TOTAL_INGESTED, str(total))
        conn.commit()
    except Exception:
        log.exception("failed to record export scheduler stats")


def _is_export_file(path: Path) -> bool:
    """Check if a file appears to be an AI assistant export."""
    if not path.is_file():
        return False

    # Check by extension
    if path.suffix.lower() in (".json", ".zip"):
        return True

    # Check by filename patterns
    name_lower = path.name.lower()
    export_patterns = (
        "chatgpt",
        "claude",
        "copilot",
        "gemini",
        "export",
        "conversations",
    )
    return any(pattern in name_lower for pattern in export_patterns)


def _should_ingest_export(conn, path: Path) -> bool:
    """True when the export file is new or changed since last ingest."""
    from store import get_source_by_path, sha256_of_file

    spath = str(path.expanduser().resolve())
    existing = get_source_by_path(conn, spath)
    if existing is None:
        return True
    try:
        digest = sha256_of_file(path)
    except OSError:
        return True
    return existing.sha256 != digest


def _ingest_export_file(path: Path, data_dir, conn) -> dict:
    """Ingest a single export file."""
    from ingest import ingest_file

    try:
        result = ingest_file(conn, path)
        return {
            "path": str(path),
            "success": not result.skipped and result.source_id is not None,
            "skipped": result.skipped,
            "source_id": result.source_id,
            "chunks": result.chunk_count,
            "reason": result.reason,
        }
    except Exception as e:
        log.exception("failed to ingest export %s", path)
        return {
            "path": str(path),
            "success": False,
            "error": str(e),
        }


def tick(data_dir, conn_factory: Callable) -> dict:
    """Check for new export files and ingest them."""
    if os.environ.get("MINION_DISABLE_EXPORT_SCHEDULER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return {"status": "disabled"}

    watch_path = export_watch_path(data_dir)
    if not watch_path or not watch_path.exists():
        return {"status": "no_watch_path", "path": str(watch_path)}

    conn = conn_factory()
    try:
        new_exports = []
        for path in watch_path.iterdir():
            if not _is_export_file(path):
                continue
            if _should_ingest_export(conn, path):
                new_exports.append(path)

        if not new_exports:
            _record_tick_stats(conn, 0)
            return {"status": "no_new_exports", "watched": str(watch_path)}

        results = []
        for path in new_exports:
            result = _ingest_export_file(path, data_dir, conn)
            results.append(result)
            if result.get("success"):
                conn.commit()
            else:
                conn.rollback()

        successful = sum(1 for r in results if r.get("success"))
        failed = len(results) - successful
        _record_tick_stats(conn, successful)

        log.info(
            "export scheduler: ingested %d/%d exports from %s",
            successful,
            len(results),
            watch_path,
        )

        return {
            "status": "completed",
            "watch_path": str(watch_path),
            "total": len(results),
            "successful": successful,
            "failed": failed,
            "results": results,
        }
    except Exception:
        log.exception("export scheduler tick failed")
        conn.rollback()
        return {
            "status": "error",
            "error": "export scheduler tick failed",
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _run_once(data_dir, conn_factory: Callable) -> None:
    tick(data_dir, conn_factory)


def _loop(data_dir, conn_factory: Callable) -> None:
    while not _stop.is_set():
        interval = export_interval_sec(data_dir)
        _run_once(data_dir, conn_factory)
        if _stop.wait(interval):
            break


def start_export_scheduler(data_dir, conn_factory: Callable) -> None:
    """Start the export monitoring scheduler."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return

    if os.environ.get("MINION_DISABLE_EXPORT_SCHEDULER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        log.info("Export scheduler disabled")
        return

    _stop.clear()
    _thread = threading.Thread(
        target=_loop,
        args=(data_dir, conn_factory),
        name="minion-export-scheduler",
        daemon=True,
    )
    _thread.start()
    log.info(
        "Export scheduler started (interval=%ss, watch_path=%s)",
        export_interval_sec(data_dir),
        export_watch_path(data_dir),
    )


def stop_export_scheduler() -> None:
    """Stop the export monitoring scheduler."""
    _stop.set()


def trigger_manual_export(data_dir, conn_factory: Callable, export_path: Optional[str] = None) -> dict:
    """Manually trigger export ingestion for a specific file or all new exports."""
    if export_path:
        path = Path(export_path).expanduser().resolve()
        if not path.exists():
            return {"status": "error", "error": f"File not found: {export_path}"}

        conn = conn_factory()
        try:
            result = _ingest_export_file(path, data_dir, conn)
            if result.get("success"):
                conn.commit()
                _record_tick_stats(conn, 1)
            else:
                conn.rollback()
                _record_tick_stats(conn, 0)
            return result
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return tick(data_dir, conn_factory)
