"""Unified activity river: council proposals + observations + suggestions."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

from council_engine import council_subject_ids_with_open, list_open_feed_items
from second_brain import build_working_context
from life_graph import WIKI_PAGE_TYPE_TO_NODE
from store import (
    ambient_events_since,
    graph_scaffold_list,
    identity_claim_list,
    sync_job_runs_recent,
    system_issues_open,
    task_list,
    wiki_page_list,
)


def _feed_item(
    *,
    feed_id: str,
    ts: float,
    lane: str,
    kind: str,
    title: str,
    body: str = "",
    parse: Optional[Dict[str, Any]] = None,
    actions: Optional[List[Dict[str, str]]] = None,
    refs: Optional[Dict[str, str]] = None,
    graph_kinds: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "item_kind": "river",
        "feed_id": feed_id,
        "ts": ts,
        "lane": lane,
        "kind": kind,
        "title": title,
        "body": body,
        "parse": parse,
        "actions": actions or [],
        "refs": refs or {},
        "graph_kinds": graph_kinds or [],
    }


_AMBIENT_RIVER_SKIP = frozenset(
    {"fs_event", "process_snapshot", "app_launched", "window_snapshot", "ax_content_changed"}
)
_FEED_SKIP_APPS = frozenset(
    {
        "",
        "minion",
        "minion-desktop",
        "cursor",
        "finder",
        "iterm2",
        "terminal",
        "system settings",
    }
)
_HIGH_SIGNAL_KINDS = frozenset(
    {
        "forty_two",
        "you",
        "graph_update",
        "graph_status",
        "inferred_task",
        "identity_claim",
        "health_issue",
        "wiki_page",
        "listening_wake",
    }
)


def _skip_ambient_payload(payload: Dict[str, Any]) -> bool:
    app = str(payload.get("app_name") or payload.get("app") or "").strip().lower()
    title = str(payload.get("window_title") or payload.get("title") or "").strip().lower()
    if app in _FEED_SKIP_APPS:
        return True
    if "minion" in title and any(x in title for x in ("open minion", "quit minion", "no graph")):
        return True
    return False


def _is_noise_sync(item: Dict[str, Any]) -> bool:
    title = str(item.get("title") or "")
    body = str(item.get("body") or "")
    if "ambient_loop" not in title:
        return False
    if "status=error" in body:
        return False
    return True


def _dedupe_ambient(items: List[Dict[str, Any]], *, max_items: int) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for item in sorted(items, key=lambda x: float(x.get("ts") or 0), reverse=True):
        title = str(item.get("title") or "")
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max_items:
            break
    return out


def _curate_river(river: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    high: List[Dict[str, Any]] = []
    low: List[Dict[str, Any]] = []
    for item in river:
        kind = str(item.get("kind") or "")
        lane = str(item.get("lane") or "")
        if kind == "sync_run" and _is_noise_sync(item):
            continue
        if kind in ("window_focus", "browser_visit", "focus"):
            low.append(item)
        elif kind in _HIGH_SIGNAL_KINDS or lane in ("conversation", "suggestion"):
            high.append(item)
        elif kind == "sync_run":
            high.append(item)
        else:
            low.append(item)
    high.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
    low = _dedupe_ambient(low, max_items=min(5, max(0, limit - len(high))))
    out = high + low
    out.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
    return out[:limit]


def _graph_status_item(conn, data_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        from graph_ambient import build_graph_spine
        from graph_fill import pick_next_gap

        spine = build_graph_spine(conn, data_dir, max_active=5)
    except Exception:
        log.debug("graph status item skipped", exc_info=True)
        return None

    totals = spine.get("totals") or {}
    active = spine.get("active_nodes") or []
    buckets = [b for b in (spine.get("buckets") or []) if int(b.get("filled_count") or 0) > 0]
    lines: List[str] = []
    if totals:
        bits = ", ".join(f"{v} {k}" for k, v in sorted(totals.items(), key=lambda x: -x[1])[:5])
        lines.append(f"Filled so far: {bits}.")
    for bucket in buckets[:3]:
        names = ", ".join(m.get("title") for m in bucket.get("members", [])[:4] if m.get("title"))
        if names:
            lines.append(f"{bucket.get('title')}: {names}.")
    if active:
        recent = ", ".join(n.get("title") for n in active[:4] if n.get("title"))
        if recent:
            lines.append(f"Recently active: {recent}.")
    gap = pick_next_gap(conn, data_dir)
    if gap:
        prompt = str(gap.get("prompt") or gap.get("title") or gap.get("field") or "").strip()
        if prompt:
            lines.append(f"Still open: {prompt[:160]}.")
    if not lines:
        lines.append("Graph scaffold is ready — waiting for life to fill it in.")

    ts = time.time()
    for node in active:
        ts = max(ts, float(node.get("updated_at") or 0))
    return _feed_item(
        feed_id="graph-status",
        ts=ts,
        lane="conversation",
        kind="graph_status",
        title="Graph status",
        body="\n".join(lines),
        graph_kinds=["person", "project", "organization"],
    )


def _ambient_to_items(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in events:
        et = str(e.get("event_type") or "window_focus")
        if et in _AMBIENT_RIVER_SKIP:
            continue
        payload = e.get("payload") or {}
        if _skip_ambient_payload(payload):
            continue
        ts = float(e.get("captured_at") or 0)
        app = str(payload.get("app_name") or "?")
        title = str(payload.get("window_title") or "")
        ax = payload.get("ax_text_sample")
        parse: Optional[Dict[str, Any]] = None
        feed_kind = et
        river_title = f"Ambient · {et}"

        if et == "window_focus":
            url = str(payload.get("url") or "")
            host = str(payload.get("url_or_host") or "")
            parse = {"status": "stored", "reason": "focus logged"}
            if host or url:
                river_title = f"Browser · {host or title[:60] or app}"
                body = url[:200] if url else ""
            else:
                label = f"{app}" + (f" — {title}" if title else "")
                river_title = f"Now · {label}"
                body = ""
        elif et == "window_snapshot":
            label = f"{app}" + (f" — {title[:60]}" if title else "")
            river_title = f"Screen · {label}"
            shot = str(payload.get("screenshot_inbox_rel") or "")
            body = (str(ax)[:320] if ax else "")
            if shot and not ax:
                parse = {"status": "stored", "reason": "captured screen (OCR pending)"}
                body = shot
            elif ax:
                parse = {
                    "status": "indexed",
                    "reason": f"visible window ({len(str(ax))} chars)",
                }
            else:
                parse = {"status": "stored", "reason": "visible window (metadata only)"}
            feed_kind = "window_snapshot"
        elif et == "ax_content_changed":
            river_title = f"AX update · {app}" + (f" — {title[:40]}" if title else "")
            body = f"hash {payload.get('ax_hash', '')[:12]}"
            parse = {"status": "stored", "reason": "Accessibility text changed"}
        elif et == "browser_visit":
            url = str(payload.get("url") or "")
            host = str(payload.get("url_or_host") or payload.get("host") or title)
            label = host[:80] if host else (title[:80] if title else app)
            river_title = f"Browser · {label}"
            body = url[:200] if url else str(payload.get("confidence") or "")
            parse = {"status": "stored", "reason": "browser_visit"}
            feed_kind = "browser_visit"
        elif et == "fs_event":
            path = str(payload.get("path_display") or payload.get("path") or "?")
            op = str(payload.get("op") or "mod")
            river_title = f"File · {op} — {path}"
            body = ""
            parse = {"status": "stored", "reason": "fs_event"}
            feed_kind = "fs_event"
        elif et == "app_launched":
            river_title = f"Launched · {payload.get('app_name') or app}"
            body = str(payload.get("bundle_id") or "")
            parse = {"status": "stored", "reason": "app_launched"}
            feed_kind = "app_launched"
        elif et == "process_snapshot":
            n = len(payload.get("apps") or [])
            river_title = f"Processes · {n} apps sampled"
            body = str(payload.get("foreground_app") or "")
            parse = {"status": "stored", "reason": "process_snapshot"}
            feed_kind = "process_snapshot"
        elif et == "listening_wake":
            river_title = "Wake · Minion mentioned"
            body = str(payload.get("transcript_excerpt") or "")[:200]
            parse = {"status": "stored", "reason": "listening_wake"}
            feed_kind = "listening_wake"
        elif et.startswith("listening") or et.startswith("full_listening"):
            river_title = f"Listening · {et.replace('_', ' ')}"
            body = str(payload.get("transcript_excerpt") or "")[:200]
            parse = {"status": "indexed" if body else "stored", "reason": et}
            feed_kind = et
        else:
            body = json.dumps(payload)[:120] if payload else ""
            parse = {"status": "stored", "reason": et}

        out.append(
            _feed_item(
                feed_id=f"amb-{e.get('event_id')}",
                ts=ts,
                lane="observed",
                kind=feed_kind,
                title=river_title,
                body=body,
                parse=parse,
                graph_kinds=["project", "job"],
            )
        )
    return out


def _sync_runs_to_items(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in runs:
        sk = str(r.get("source_key") or "sync")
        n = int(r.get("items_count") or 0)
        err = r.get("error")
        status = str(r.get("status") or "ok")
        ts = float(r.get("finished_at") or r.get("started_at") or 0)
        if err:
            parse = {"status": "error", "reason": str(err)[:240]}
        elif n > 0:
            parse = {"status": "indexed", "reason": f"{n} item(s) processed"}
        else:
            parse = {"status": "skipped", "reason": "no new items"}
        out.append(
            _feed_item(
                feed_id=f"sync-{r.get('run_id')}",
                ts=ts,
                lane="parsed",
                kind="sync_run",
                title=f"Sync · {sk}",
                body=f"status={status}",
                parse=parse,
            )
        )
    return out


def _wiki_to_items(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in pages:
        pt = str(p.get("page_type") or "topic")
        gk = WIKI_PAGE_TYPE_TO_NODE.get(pt)
        out.append(
            _feed_item(
                feed_id=f"wiki-{p.get('page_id')}",
                ts=float(p.get("last_updated") or 0),
                lane="parsed",
                kind="wiki_page",
                title=f"Knowledge · {p.get('title')}",
                body=(p.get("body_md") or "")[:280],
                parse={"status": "stored", "reason": f"wiki page_type={pt}"},
                refs={"page_id": str(p.get("page_id") or "")},
                graph_kinds=[gk] if gk else [],
            )
        )
    return out


def _task_suggestions(
    tasks: List[Dict[str, Any]], *, council_subjects: set[str]
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in tasks:
        if t.get("origin") not in ("inferred", "agent"):
            continue
        if t.get("status") not in ("open", "review"):
            continue
        subj = str(t.get("subject_id") or t.get("linked_node_id") or "")
        if subj and subj in council_subjects:
            continue
        out.append(
            _feed_item(
                feed_id=f"task-{t.get('task_id')}",
                ts=float(t.get("updated_at") or t.get("created_at") or 0),
                lane="suggestion",
                kind="inferred_task",
                title=str(t.get("title") or "Inferred task"),
                body=(t.get("body_md") or "")[:400],
                actions=[
                    {"id": "accept", "label": "Accept"},
                    {"id": "dismiss", "label": "Dismiss"},
                ],
                refs={"task_id": str(t.get("task_id") or "")},
                graph_kinds=["project", "job"],
            )
        )
    return out


def _claim_suggestions(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in claims:
        if c.get("status") != "proposed":
            continue
        out.append(
            _feed_item(
                feed_id=f"claim-{c.get('claim_id')}",
                ts=float(c.get("updated_at") or c.get("created_at") or 0),
                lane="suggestion",
                kind="identity_claim",
                title=f"Mirror · {c.get('kind')}",
                body=(c.get("text") or "")[:400],
                actions=[
                    {"id": "approve", "label": "Approve"},
                    {"id": "reject", "label": "Reject"},
                ],
                refs={"claim_id": str(c.get("claim_id") or "")},
                graph_kinds=["person", "family", "group"],
            )
        )
    return out


def _issue_suggestions(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i in issues:
        out.append(
            _feed_item(
                feed_id=f"issue-{i.get('issue_id')}",
                ts=float(i.get("created_at") or 0),
                lane="suggestion",
                kind="health_issue",
                title=f"Needs attention · {i.get('severity')}",
                body=(i.get("body_md") or "")[:400],
                actions=[{"id": "resolve", "label": "Dismiss"}],
                refs={"issue_id": str(i.get("issue_id") or "")},
            )
        )
    return out


def build_activity_feed(
    conn,
    data_dir: Path,
    *,
    limit: int = 80,
    since_hours: float = 48.0,
) -> Dict[str, Any]:
    """Merge observations, parse outcomes, and suggestions into one chronological river."""
    since_ts = time.time() - since_hours * 3600.0
    lim = int(max(10, min(limit, 200)))

    try:
        from graph_phantom import purge_phantom_graph_artifacts

        purge_phantom_graph_artifacts(conn, commit=True)
    except Exception:
        log.debug("phantom graph purge skipped", exc_info=True)

    working = build_working_context(conn, data_dir, for_mcp=False, memory_top_k=3)
    focus = working.get("focus")
    now_item: Optional[Dict[str, Any]] = None
    if focus:
        app = str(focus.get("app_name") or "").strip()
        title = str(focus.get("window_title") or "").strip()
        if app or title:
            now_item = {
                **_feed_item(
                    feed_id="now-focus",
                    ts=float(focus.get("ts") or time.time()),
                    lane="now",
                    kind="focus",
                    title=f"{app}{' — ' + title if title else ''}".strip() or "Focused window",
                    body="",
                    graph_kinds=["project", "job"],
                ),
            }

    council_subjects = council_subject_ids_with_open(conn)
    council_items = list_open_feed_items(conn, data_dir)

    ambient = ambient_events_since(conn, since_ts=since_ts, limit=lim)
    runs = sync_job_runs_recent(conn, limit=min(30, lim))
    wiki = wiki_page_list(conn, status="active", limit=min(20, lim))
    tasks = task_list(conn, limit=50)
    claims = identity_claim_list(conn, status="proposed", limit=30)
    issues = system_issues_open(conn, limit=10)

    river: List[Dict[str, Any]] = []
    forty_two_state: Dict[str, Any] = {
        "active_thread_id": None,
        "needs_question": False,
        "open_count": 0,
    }
    try:
        from forty_two import conversation_feed_items, pinned_conversation_item, stream_state

        river.extend(conversation_feed_items(conn, since_ts=since_ts, limit=min(40, lim)))
        forty_two_state = stream_state(conn, data_dir)
        pin = pinned_conversation_item(conn)
        if pin:
            pin_ids = {pin["feed_id"]}
            river = [r for r in river if r.get("feed_id") not in pin_ids]
            river.insert(0, pin)
    except Exception:
        log.exception("forty_two feed items failed")
    try:
        from graph_events import graph_event_feed_items

        river.extend(graph_event_feed_items(data_dir, since_ts=since_ts, limit=min(30, lim)))
    except Exception:
        log.debug("graph event feed skipped", exc_info=True)
    river.extend(_ambient_to_items(ambient))
    river.extend(_sync_runs_to_items(runs))
    river.extend(_wiki_to_items(wiki))
    river.extend(_task_suggestions(tasks, council_subjects=council_subjects))
    river.extend(_claim_suggestions(claims))
    river.extend(_issue_suggestions(issues))

    status = _graph_status_item(conn, data_dir)
    if status:
        river.insert(1 if pin else 0, status)

    river = _curate_river(river, limit=lim)

    items: List[Dict[str, Any]] = []
    items.extend(council_items)
    items.extend(river)

    return {
        "now": now_item,
        "items": items,
        "council_items": council_items,
        "memory_prefetch": [],
        "graph": graph_scaffold_list(conn),
        "forty_two": forty_two_state,
        "composed_at": time.time(),
        "since_ts": since_ts,
    }
