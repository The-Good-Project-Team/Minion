"""Session open: briefing since last visit + exactly one Minion request."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

SESSION_FILENAME = "session.json"
_HINT_TTL_SEC = 30.0

_cached_hint: Optional[Dict[str, Any]] = None
_cached_hint_at: float = 0.0


def _session_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / SESSION_FILENAME


def load_session_file(data_dir: Path) -> Dict[str, Any]:
    p = _session_path(data_dir)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def save_session_file(data_dir: Path, data: Dict[str, Any]) -> None:
    p = _session_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_session_hint() -> Optional[Dict[str, Any]]:
    """Recent session/open payload for feed + context bundle (in-process TTL)."""
    global _cached_hint, _cached_hint_at
    if _cached_hint and (time.time() - _cached_hint_at) < _HINT_TTL_SEC:
        return dict(_cached_hint)
    return None


def session_hint_from_disk(data_dir: Path) -> Optional[Dict[str, Any]]:
    last = load_session_file(data_dir).get("last_open")
    if not isinstance(last, dict):
        return None
    return {
        "briefing_summary": (last.get("briefing_md") or "")[:400],
        "request_kind": last.get("request_kind"),
        "request_preview": (last.get("request_md") or "")[:200],
        "opened_at": last.get("opened_at"),
        "thread_id": last.get("thread_id"),
    }


def _cache_hint(payload: Dict[str, Any]) -> None:
    global _cached_hint, _cached_hint_at
    _cached_hint = {
        "briefing_summary": (payload.get("briefing_md") or "")[:400],
        "request_kind": payload.get("request_kind"),
        "request_preview": (payload.get("request_md") or "")[:200],
        "opened_at": payload.get("opened_at"),
        "thread_id": payload.get("thread_id"),
    }
    _cached_hint_at = time.time()


def build_delta(conn, data_dir: Path, since_ts: float) -> Dict[str, Any]:
    from store import ambient_events_since

    since = float(since_ts)
    ambient = ambient_events_since(conn, since_ts=since, limit=120)
    app_counts: Dict[str, int] = {}
    for ev in ambient:
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(ev.get("payload_json") or "{}")
            except Exception:
                payload = {}
        app = str(payload.get("app_name") or payload.get("app") or ev.get("event_type") or "").strip()
        if app:
            app_counts[app] = app_counts.get(app, 0) + 1
    top_apps = sorted(app_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    node_row = conn.execute(
        "SELECT COUNT(*) AS n FROM graph_nodes WHERE updated_at >= ? AND status NOT IN ('scaffold', 'stub')",
        (since,),
    ).fetchone()
    cand_row = conn.execute(
        "SELECT COUNT(*) AS n FROM graph_candidates WHERE created_at >= ?",
        (since,),
    ).fetchone()
    source_row = conn.execute(
        "SELECT COUNT(*) AS n FROM sources WHERE updated_at >= ?",
        (since,),
    ).fetchone()

    return {
        "since_ts": since,
        "ambient_event_count": len(ambient),
        "top_apps": [{"app": a, "events": c} for a, c in top_apps],
        "graph_nodes_updated": int(node_row["n"] if node_row else 0),
        "graph_candidates_created": int(cand_row["n"] if cand_row else 0),
        "sources_updated": int(source_row["n"] if source_row else 0),
    }


def _compose_briefing_md(delta: Dict[str, Any], *, display_name: str = "") -> str:
    parts: List[str] = []
    greet = f"Welcome back{', ' + display_name if display_name.strip() else ''}."
    parts.append(greet)

    ambient_n = int(delta.get("ambient_event_count") or 0)
    if ambient_n:
        apps = delta.get("top_apps") or []
        if apps:
            top = ", ".join(f"{a['app']} ({a['events']})" for a in apps[:3])
            parts.append(f"Since you were here: {ambient_n} ambient capture event(s), mostly {top}.")
        else:
            parts.append(f"Since you were here: {ambient_n} new ambient capture event(s).")
    nodes = int(delta.get("graph_nodes_updated") or 0)
    cands = int(delta.get("graph_candidates_created") or 0)
    sources = int(delta.get("sources_updated") or 0)
    graph_bits: List[str] = []
    if nodes:
        graph_bits.append(f"{nodes} graph update(s)")
    if cands:
        graph_bits.append(f"{cands} new candidate(s)")
    if sources:
        graph_bits.append(f"{sources} new source(s)")
    if graph_bits:
        parts.append("Graph/vault: " + ", ".join(graph_bits) + ".")
    if len(parts) == 1:
        parts.append("Nothing major changed locally since your last visit — your graph is in good shape.")
    return " ".join(parts)


def _active_graph_question(conn) -> Tuple[Optional[str], Optional[str], str]:
    """Returns (thread_id, question_md, request_kind)."""
    from librarian import active_thread

    full = active_thread(conn)
    if not full:
        return None, None, ""
    msgs = full.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") != "assistant":
            continue
        body = str(m.get("body_md") or "").strip()
        if body:
            return str(full["thread_id"]), body, "graph_active"
    return str(full["thread_id"]), None, "graph_active"


def _resolve_request(
    conn,
    data_dir: Path,
    *,
    delta: Dict[str, Any],
) -> Dict[str, Any]:
    from connector_intent import list_open_connector_work, next_poll_question
    from librarian import active_thread
    from graph_fill import open_thread_for_gap, pick_next_gap

    tid, qbody, kind = _active_graph_question(conn)
    if tid and qbody:
        return {
            "request_kind": kind,
            "request_md": qbody,
            "thread_id": tid,
            "created_thread": False,
        }

    if tid and not qbody:
        return {
            "request_kind": "graph_active",
            "request_md": "Pick up where we left off — add a short answer when you are ready.",
            "thread_id": tid,
            "created_thread": False,
        }

    gap = pick_next_gap(conn, data_dir)
    if gap:
        out = open_thread_for_gap(conn, gap, data_dir=data_dir)
        thread = out.get("thread") or {}
        new_tid = thread.get("thread_id")
        msgs = thread.get("messages") or []
        body = ""
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                body = str(m.get("body_md") or "").strip()
                break
        if not body:
            label = gap.get("label") or gap.get("bucket_label") or "this"
            body = f"Who is **{label}** to you, and how do you know them?"
        return {
            "request_kind": "graph_gap",
            "request_md": body,
            "thread_id": new_tid,
            "created_thread": bool(out.get("created")),
            "gap": gap,
        }

    connectors = list_open_connector_work(conn, limit=1)
    if connectors:
        c = connectors[0]
        title = c.get("title") or "Connect a source"
        hint = c.get("import_hint") or ""
        body = f"**{title}** — want me to scaffold a local connector?"
        if hint:
            body += f"\n\n{hint}"
        return {
            "request_kind": "connector",
            "request_md": body,
            "thread_id": None,
            "created_thread": False,
            "connector": c,
        }

    poll = next_poll_question(data_dir)
    if poll:
        return {
            "request_kind": "resource_poll",
            "request_md": str(poll.get("question") or "Do you use this data source?"),
            "thread_id": None,
            "created_thread": False,
            "poll": poll,
        }

    if int(delta.get("ambient_event_count") or 0) > 0:
        apps = delta.get("top_apps") or []
        app_name = apps[0]["app"] if apps else "your apps"
        return {
            "request_kind": "ambient_nudge",
            "request_md": (
                f"You were active in **{app_name}** since last time. "
                "Want to tie that work to a person or project in your graph?"
            ),
            "thread_id": None,
            "created_thread": False,
        }

    return {
        "request_kind": "idle_prompt",
        "request_md": (
            "Your graph looks caught up. What should I connect or watch next — "
            "Gmail, Slack, a folder, or something else?"
        ),
        "thread_id": None,
        "created_thread": False,
    }


def open_session(
    conn,
    data_dir: Path,
    *,
    display_name: str = "",
) -> Dict[str, Any]:
    """Compute briefing + one request; advance last_app_open_at."""
    data_dir = Path(data_dir).expanduser().resolve()
    state = load_session_file(data_dir)
    now = time.time()
    last_open_at = float(state.get("last_app_open_at") or 0.0)
    if last_open_at <= 0:
        last_open_at = now - 24 * 3600.0

    delta = build_delta(conn, data_dir, last_open_at)
    req = _resolve_request(conn, data_dir, delta=delta)
    briefing_md = _compose_briefing_md(delta, display_name=display_name)
    request_md = str(req.get("request_md") or "").strip()

    payload: Dict[str, Any] = {
        "ok": True,
        "briefing_md": briefing_md,
        "request_md": request_md,
        "request_kind": req.get("request_kind"),
        "thread_id": req.get("thread_id"),
        "created_thread": bool(req.get("created_thread")),
        "delta_summary": {
            "ambient_event_count": delta.get("ambient_event_count"),
            "graph_nodes_updated": delta.get("graph_nodes_updated"),
            "top_apps": delta.get("top_apps"),
        },
        "last_open_at": last_open_at,
        "opened_at": now,
    }

    state["last_app_open_at"] = now
    state["last_open"] = {
        "briefing_md": briefing_md,
        "request_md": request_md,
        "request_kind": payload["request_kind"],
        "thread_id": payload.get("thread_id"),
        "opened_at": now,
    }
    save_session_file(data_dir, state)
    _cache_hint(payload)

    if payload.get("thread_id"):
        try:
            import api as api_mod
            from chat_store import chat_open_count

            api_mod._schedule_broadcast(
                {
                    "type": "chat_updated",
                    "thread_id": payload["thread_id"],
                    "open_count": chat_open_count(conn),
                }
            )
        except Exception:
            log.debug("session open chat broadcast skipped", exc_info=True)

    return payload
