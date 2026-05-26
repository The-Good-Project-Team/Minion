"""Background butler jobs: ambient sync + AX indexing (no user scheduling)."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from ambient_index import index_ax_from_stream
from ambient_pipeline import ingest_ambient_jsonl

log = logging.getLogger(__name__)

_INTERVAL_SEC = 45.0
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _run_once(data_dir, conn_factory) -> None:
    started = time.time()
    conn = conn_factory()
    try:
        amb = ingest_ambient_jsonl(data_dir=data_dir, conn=conn, max_lines=500)
        conn.commit()
        ax = index_ax_from_stream(data_dir=data_dir, conn=conn, dry_run=False)
        conn.commit()
        if os.environ.get("MINION_ENABLE_VOICE", "").strip().lower() in ("1", "true", "on"):
            try:
                from listening_ingest import ingest_listening_segments

                ingest_listening_segments(data_dir=data_dir, conn=conn)
            except Exception:
                log.exception("listening ingest failed")
        try:
            from memory_lifecycle import run_auto_maintenance, run_lightweight_cleanup

            run_lightweight_cleanup(conn)
            run_auto_maintenance(conn, data_dir)
        except Exception:
            log.exception("memory_lifecycle failed")
        conn.commit()
        from store import sync_job_run_append, sync_source_touch

        sync_source_touch(conn, "screen_context", status="healthy", items=amb.get("ingested", 0))
        sync_source_touch(conn, "ambient_ax_index", status="healthy", items=ax.get("indexed", 0))
        sync_job_run_append(
            conn,
            source_key="ambient_loop",
            status="ok",
            started_at=started,
            finished_at=time.time(),
            items_count=int(amb.get("ingested", 0)) + int(ax.get("indexed", 0)),
        )
        if not os.environ.get("MINION_DISABLE_AMBIENT_SCHEDULER"):
            graph_signal = int(ax.get("indexed", 0) or 0) > 0
            try:
                from life_evidence_index import ingest_life_evidence

                ev = ingest_life_evidence(data_dir, conn)
                graph_signal = graph_signal or int(ev.get("contacts", 0) or 0) > 0
            except Exception:
                log.exception("life evidence ingest failed")
            try:
                from council_engine import evaluate_patterns

                evaluate_patterns(conn, data_dir)
            except Exception:
                log.exception("council evaluate_patterns failed")
            if graph_signal:
                try:
                    from forty_two_queue import enqueue_graph_infer

                    enqueue_graph_infer(conn, reason="ambient")
                except Exception:
                    log.exception("graph infer enqueue after ambient failed")
        conn.commit()
    except Exception:
        log.exception("ambient scheduler tick failed")
        try:
            from store import sync_job_run_append, system_issue_upsert

            sync_job_run_append(
                conn,
                source_key="ambient_loop",
                status="error",
                started_at=started,
                finished_at=time.time(),
                items_count=0,
                error="ambient scheduler tick failed",
            )
            system_issue_upsert(
                conn,
                issue_id="ambient-loop-error",
                severity="warning",
                source_key="ambient_loop",
                body_md="Background ambient sync failed. Check sidecar log; restart Minion if persistent.",
            )
            conn.commit()
        except Exception:
            log.exception("failed to record ambient scheduler error")


def _loop(data_dir, conn_factory) -> None:
    while not _stop.wait(_INTERVAL_SEC):
        _run_once(data_dir, conn_factory)


def start_ambient_scheduler(data_dir, conn_factory) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_loop,
        args=(data_dir, conn_factory),
        name="minion-ambient-scheduler",
        daemon=True,
    )
    _thread.start()
    log.info("ambient scheduler started (interval=%ss)", _INTERVAL_SEC)


def stop_ambient_scheduler() -> None:
    _stop.set()
