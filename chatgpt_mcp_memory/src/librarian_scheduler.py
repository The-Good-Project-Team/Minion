"""Background cadence: queue the next 42 question when idle."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SEC = 300.0
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def librarian_interval_sec(data_dir: Optional[Path] = None) -> float:
    raw = os.environ.get("MINION_LIBRARIAN_INTERVAL_SEC", "").strip()
    if raw:
        try:
            return max(60.0, float(raw))
        except ValueError:
            pass
    if data_dir:
        try:
            from settings import load_settings

            v = load_settings(Path(data_dir)).get("librarian_interval_sec")
            if v is not None:
                return max(60.0, float(v))
        except Exception:
            pass
    return _DEFAULT_INTERVAL_SEC


def _notify_chat_updated(conn, thread_id: str) -> None:
    try:
        import api as api_mod
        from chat_store import chat_open_count

        api_mod._schedule_broadcast(
            {
                "type": "chat_updated",
                "thread_id": thread_id,
                "open_count": chat_open_count(conn),
            }
        )
    except Exception:
        log.debug("Librarian scheduler chat broadcast skipped", exc_info=True)


def tick(conn, data_dir) -> Optional[str]:
    """Open a new 42 thread when idle and a gap exists. Returns thread_id if created."""
    if os.environ.get("MINION_DISABLE_LIBRARIAN_SCHEDULER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return None

    from gemini_client import gemini_configured

    if not gemini_configured(data_dir):
        return None

    try:
        from settings import load_settings

        if load_settings(Path(data_dir)).get("librarian_enabled") is False:
            return None
    except Exception:
        pass

    from librarian import active_thread
    from librarian_queue import drain_graph_infer_queue
    from graph_fill import pick_next_gap, open_thread_for_gap

    if active_thread(conn):
        return None

    drain = drain_graph_infer_queue(conn, data_dir, max_gaps=5)
    if drain.get("filled", 0):
        conn.commit()
        return None

    if active_thread(conn):
        return None

    gap = pick_next_gap(conn, data_dir)
    if not gap:
        return None

    out = open_thread_for_gap(conn, gap, data_dir=data_dir)
    tid = out["thread"]["thread_id"]
    _notify_chat_updated(conn, tid)
    log.info("Librarian scheduler opened thread %s gap=%s", tid, gap.get("gap_type"))
    return tid


def _run_once(data_dir, conn_factory: Callable) -> None:
    conn = conn_factory()
    try:
        tid = tick(conn, data_dir)
        if tid:
            conn.commit()
    except Exception:
        log.exception("Librarian scheduler tick failed")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _loop(data_dir, conn_factory: Callable) -> None:
    while not _stop.is_set():
        interval = librarian_interval_sec(data_dir)
        if _stop.wait(interval):
            break
        _run_once(data_dir, conn_factory)


def start_librarian_scheduler(data_dir, conn_factory: Callable) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if os.environ.get("MINION_DISABLE_LIBRARIAN_SCHEDULER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        log.info("Librarian scheduler disabled")
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_loop,
        args=(data_dir, conn_factory),
        name="minion-42-scheduler",
        daemon=True,
    )
    _thread.start()
    log.info(
        "Librarian scheduler started (interval=%ss, gemini required)",
        librarian_interval_sec(data_dir),
    )


def stop_librarian_scheduler() -> None:
    _stop.set()
