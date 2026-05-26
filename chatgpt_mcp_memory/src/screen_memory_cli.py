"""CLI entrypoints for screen-first memory."""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from screen_memory import (
    as_json,
    create_task_from_recent_screen,
    miyagi_guidance,
    remember_screen,
    screen_search,
    screen_memory_status,
    summarize_last,
    verify_screen_memory_pipeline,
    what_was_i_doing,
)
from store import DB_FILENAME, connect, seed_sync_sources


def _conn(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    return connect(data_dir / DB_FILENAME)


def _duration_minutes(value: str | None, default: int) -> int:
    if not value:
        return default
    raw = str(value).strip().lower()
    if raw.endswith("m"):
        raw = raw[:-1]
    elif raw.endswith("h"):
        return max(1, int(float(raw[:-1]) * 60))
    return max(1, int(float(raw)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="screen_memory_cli")
    p.add_argument("--data-dir", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("remember")
    r.add_argument("--max-lines", type=int, default=1200)
    r.add_argument("--no-screenshots", action="store_true")
    r.add_argument("--no-adapters", action="store_true")

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=8)
    s.add_argument("--app", default="")
    s.add_argument("--after", type=float, default=None)
    s.add_argument("--before", type=float, default=None)

    sm = sub.add_parser("summarize-last")
    sm.add_argument("duration", nargs="?")
    sm.add_argument("--minutes", type=int, default=30)

    w = sub.add_parser("what-was-i-doing")
    w.add_argument("duration", nargs="?")
    w.add_argument("--minutes", type=int, default=20)

    g = sub.add_parser("guidance")
    g.add_argument("--minutes", type=int, default=30)

    st = sub.add_parser("status")
    st.add_argument("--minutes", type=int, default=60)
    st.add_argument("--probe", action="store_true")

    ct = sub.add_parser("create-task")
    ct.add_argument("--minutes", type=int, default=20)
    ct.add_argument("--title", default="")

    sub.add_parser("verify")

    args = p.parse_args(argv)
    if args.cmd == "verify":
        with tempfile.TemporaryDirectory(prefix="minion-screen-verify-") as tmp:
            verify_dir = Path(tmp)
            conn = connect(verify_dir / DB_FILENAME)
            try:
                seed_sync_sources(conn)
                conn.commit()
                out = verify_screen_memory_pipeline(conn, verify_dir)
                print(as_json(out))
                return 0 if out.get("ok") else 1
            finally:
                conn.close()

    data_dir = Path(args.data_dir).expanduser().resolve()
    conn = _conn(data_dir)
    try:
        if args.cmd == "remember":
            out = remember_screen(
                conn,
                data_dir,
                max_lines=args.max_lines,
                ingest_screenshots=not args.no_screenshots,
                run_adapters=not args.no_adapters,
            )
        elif args.cmd == "search":
            out = screen_search(conn, args.query, top_k=args.top_k, app=args.app, after=args.after, before=args.before)
        elif args.cmd == "summarize-last":
            out = summarize_last(conn, minutes=_duration_minutes(args.duration, args.minutes))
        elif args.cmd == "what-was-i-doing":
            out = what_was_i_doing(conn, minutes=_duration_minutes(args.duration, args.minutes))
        elif args.cmd == "guidance":
            out = miyagi_guidance(conn, data_dir, minutes=args.minutes)
        elif args.cmd == "status":
            out = screen_memory_status(conn, data_dir, minutes=args.minutes, run_probe=args.probe)
        elif args.cmd == "create-task":
            out = create_task_from_recent_screen(conn, minutes=args.minutes, title=args.title)
        else:  # pragma: no cover - argparse enforces choices.
            raise SystemExit(2)
        print(as_json(out))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
