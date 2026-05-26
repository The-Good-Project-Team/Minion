"""Ambient graph enrichment — life fills the scaffold boxes without being asked."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from entity_resolution import ensure_person_node, link_belongs_to_scaffold
from graph_events import log_graph_event
from store import ambient_events_since

log = logging.getLogger(__name__)

ME_NODE = "scaffold-me"
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
_GENERIC_HOSTS = frozenset(
    {
        "google.com",
        "www.google.com",
        "accounts.google.com",
        "localhost",
        "127.0.0.1",
    }
)
_SKIP_APPS = frozenset(
    {
        "",
        "minion",
        "minion-desktop",
        "iterm2",
        "terminal",
        "system settings",
        "finder",
        "cursor",
        "universalaccessauthwarn",
    }
)
_SKIP_TITLE_SUBSTR = frozenset(
    {
        "minion",
        "iterm",
        "system settings",
        "accessibility",
        "screen recording",
    }
)


def enrich_graph_from_ambient(
    conn,
    data_dir: Path,
    *,
    since_hours: float = 6.0,
    limit_events: int = 250,
) -> Dict[str, Any]:
    """
    Observe recent ambient events and attach life signals to the graph scaffold.
    Creates nodes only when evidence repeats or identifiers are hard (email).
    """
    since_ts = time.time() - since_hours * 3600.0
    try:
        events = ambient_events_since(conn, since_ts=since_ts, limit=limit_events)
    except Exception:
        log.debug("ambient graph enrich skipped", exc_info=True)
        return {"touched": 0, "created": 0, "signals": 0}

    title_counts: Counter[str] = Counter()
    parsed: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for event in events:
        payload = event.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        parsed.append((event, payload))
        title = _norm_title(payload.get("window_title") or payload.get("title") or "")
        app = str(payload.get("app_name") or payload.get("app") or "").strip()
        if title and not _skip_title(title, app):
            title_counts[title] += 1

    touched = 0
    created = 0
    signals = 0
    seen_keys: Set[str] = set()

    for event, payload in parsed:
        event_id = str(event.get("event_id") or "")
        event_type = str(event.get("event_type") or event.get("kind") or "")
        app = str(payload.get("app_name") or payload.get("app") or "").strip()
        ref = f"amb:{event_id}" if event_id else ""

        for email in _extract_emails(payload):
            key = f"email:{email.lower()}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            signals += 1
            label = _label_from_email(email)
            if not label:
                continue
            nid = ensure_person_node(
                conn,
                label=label,
                meta={
                    "email": email,
                    "source": "ambient_clipboard",
                    "evidence_refs": [ref] if ref else [],
                },
            )
            link_belongs_to_scaffold(conn, nid)
            if _touch_node(conn, nid, ref, app=app, title=label, data_dir=data_dir):
                touched += 1
            elif _was_new_person(conn, nid):
                created += 1
                touched += 1
                log_graph_event(
                    data_dir,
                    f"Added **{label}** to Friends from clipboard email.",
                    node_id=nid,
                    node_kind="person",
                    action="create",
                )

        host = _payload_host(payload)
        if host and host not in _GENERIC_HOSTS:
            key = f"host:{host}"
            if key not in seen_keys:
                seen_keys.add(key)
                signals += 1
                org_title = _label_from_host(host)
                if org_title:
                    org_id = _find_or_create_org(conn, org_title, data_dir=data_dir)
                    if org_id and _touch_node(
                        conn, org_id, ref, app=app, title=org_title, data_dir=data_dir
                    ):
                        touched += 1

        title = _norm_title(payload.get("window_title") or payload.get("title") or "")
        if title and not _skip_title(title, app):
            key = f"title:{title}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            signals += 1
            matched = _match_existing_nodes(conn, title)
            for nid, kind in matched:
                if _touch_node(conn, nid, ref, app=app, title=title, data_dir=data_dir):
                    touched += 1
            if matched:
                continue
            if title_counts[title] >= 2 or _looks_like_project(title):
                pid = _find_or_create_project(conn, title, data_dir=data_dir)
                if pid:
                    if _touch_node(conn, pid, ref, app=app, title=title, data_dir=data_dir):
                        touched += 1
                    if title_counts[title] >= 2:
                        created += 1
                        log_graph_event(
                            data_dir,
                            f"Linked active project **{title[:80]}** from screen attention.",
                            node_id=pid,
                            node_kind="project",
                            action="create",
                        )

    if touched or created:
        try:
            from graph_fill import persist_graph_snapshot

            persist_graph_snapshot(conn, data_dir)
        except Exception:
            log.debug("graph snapshot after ambient enrich failed", exc_info=True)

    return {
        "touched": touched,
        "created": created,
        "signals": signals,
        "events_scanned": len(events),
    }


def build_graph_spine(
    conn,
    data_dir: Optional[Path] = None,
    *,
    max_active: int = 8,
) -> Dict[str, Any]:
    """Compact graph root context: scaffold fill state + recently touched nodes."""
    from store import graph_filled_counts, graph_members_by_parent, graph_scaffold_nodes

    totals = graph_filled_counts(conn)
    members = graph_members_by_parent(conn)
    scaffold_nodes = {n["node_id"]: n for n in graph_scaffold_nodes(conn)}

    bucket_fill: List[Dict[str, Any]] = []
    for node_id, node in scaffold_nodes.items():
        if str(node.get("node_kind")) != "scaffold":
            continue
        direct = members.get(node_id, [])
        child_ids = [cid for cid, cn in scaffold_nodes.items() if cn.get("parent_node_id") == node_id]
        under = list(direct)
        for cid in child_ids:
            under.extend(members.get(cid, []))
        if not under and node_id not in (
            "scaffold-people-friends",
            "scaffold-projects-active",
            "scaffold-work-companies",
        ):
            continue
        bucket_fill.append(
            {
                "bucket_id": node_id,
                "title": node.get("title") or node_id,
                "filled_count": len(under),
                "members": [
                    {
                        "node_id": m.get("node_id"),
                        "node_kind": m.get("node_kind"),
                        "title": m.get("title"),
                        "summary_snippet": m.get("summary_snippet"),
                    }
                    for m in under[:6]
                ],
            }
        )
    bucket_fill.sort(key=lambda b: (-int(b["filled_count"]), b["title"]))

    active = _active_nodes(conn, limit=max_active)
    spine_lines = ["## Graph spine"]
    if totals:
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(totals.items())[:8])
        spine_lines.append(f"Filled: {kinds}.")
    for b in bucket_fill[:5]:
        if b["filled_count"]:
            names = ", ".join(m["title"] for m in b["members"][:4] if m.get("title"))
            spine_lines.append(f"- **{b['title']}** ({b['filled_count']}): {names or '—'}")
    if active:
        spine_lines.append("Recently active:")
        for n in active[:5]:
            hint = n.get("attention_hint") or n.get("summary_snippet") or ""
            spine_lines.append(f"- {n.get('node_kind')} **{n.get('title')}** — {hint[:80]}")

    return {
        "totals": totals,
        "buckets": bucket_fill[:12],
        "active_nodes": active,
        "spine_md": "\n".join(spine_lines),
        "generated_at": time.time(),
    }


def _active_nodes(conn, *, limit: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT node_id, node_kind, title, summary, source_refs_json, confidence, updated_at "
        "FROM graph_nodes WHERE status NOT IN ('scaffold', 'stub') "
        "ORDER BY updated_at DESC LIMIT ?",
        (max(limit * 3, 24),),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        refs = _parse_json_list(row["source_refs_json"])
        amb_refs = [r for r in refs if str(r).startswith("amb:")]
        summary = _parse_summary(row["summary"])
        attention = summary.get("recent_attention") or []
        hint = ""
        if attention:
            last = attention[0]
            hint = f"{last.get('app', '')} — {last.get('title', '')}".strip(" —")
        out.append(
            {
                "node_id": str(row["node_id"]),
                "node_kind": str(row["node_kind"]),
                "title": str(row["title"] or ""),
                "confidence": float(row["confidence"] or 0),
                "ambient_refs": len(amb_refs),
                "attention_hint": hint,
                "summary_snippet": str(summary.get("user_note") or "")[:100],
            }
        )
        if len(out) >= limit:
            break
    out.sort(
        key=lambda n: (int(n.get("ambient_refs") or 0), float(n.get("confidence") or 0)),
        reverse=True,
    )
    return out[:limit]


def _touch_node(
    conn,
    node_id: str,
    ref: str,
    *,
    app: str,
    title: str,
    data_dir: Optional[Path],
) -> bool:
    from forty_two_infer import _set_node_evidence

    row = conn.execute(
        "SELECT source_refs_json, summary FROM graph_nodes WHERE node_id=?",
        (node_id,),
    ).fetchone()
    if not row:
        return False
    refs = _parse_json_list(row["source_refs_json"])
    changed = False
    if ref and ref not in refs:
        _set_node_evidence(conn, node_id, [ref], 0.62)
        changed = True
    summary = _parse_summary(row["summary"])
    attention = list(summary.get("recent_attention") or [])
    entry = {"ts": time.time(), "app": app[:80], "title": title[:120]}
    if entry not in attention:
        attention.insert(0, entry)
        summary["recent_attention"] = attention[:5]
        conn.execute(
            "UPDATE graph_nodes SET summary=?, updated_at=? WHERE node_id=?",
            (json.dumps(summary, ensure_ascii=False), time.time(), node_id),
        )
        changed = True
    if changed and ref:
        log_graph_event(
            data_dir,
            f"Linked recent attention to **{_node_title(conn, node_id)}**.",
            node_id=node_id,
            node_kind=_node_kind(conn, node_id),
            action="touch",
        )
    return changed


def _was_new_person(conn, node_id: str) -> bool:
    row = conn.execute(
        "SELECT created_at, updated_at FROM graph_nodes WHERE node_id=?",
        (node_id,),
    ).fetchone()
    if not row:
        return False
    return abs(float(row["updated_at"]) - float(row["created_at"])) < 2.0


def _find_or_create_org(conn, title: str, *, data_dir: Optional[Path]) -> Optional[str]:
    existing = conn.execute(
        "SELECT node_id FROM graph_nodes WHERE node_kind='organization' "
        "AND lower(title)=lower(?) AND status NOT IN ('scaffold', 'stub') LIMIT 1",
        (title[:200],),
    ).fetchone()
    if existing:
        return str(existing["node_id"])
    from graph_fill import _create_graph_node, _default_parent_for_kind

    parent = _default_parent_for_kind("organization")
    nid = _create_graph_node(conn, parent, "organization", title, user_note="Seen in browser activity.")
    log_graph_event(
        data_dir,
        f"Added organization **{title}** from browsing.",
        node_id=nid,
        node_kind="organization",
        action="create",
    )
    return nid


def _find_or_create_project(conn, title: str, *, data_dir: Optional[Path]) -> Optional[str]:
    existing = conn.execute(
        "SELECT node_id FROM graph_nodes WHERE node_kind='project' "
        "AND lower(title)=lower(?) AND status NOT IN ('scaffold', 'stub') LIMIT 1",
        (title[:200],),
    ).fetchone()
    if existing:
        return str(existing["node_id"])
    from graph_fill import ME_NODE, _create_graph_node, _default_parent_for_kind, _link_knows

    parent = _default_parent_for_kind("project")
    nid = _create_graph_node(
        conn,
        parent,
        "project",
        title,
        user_note="Inferred from repeated screen focus.",
    )
    _link_knows(conn, ME_NODE, nid, "Active from ambient attention.")
    return nid


def _match_existing_nodes(conn, title: str) -> List[Tuple[str, str]]:
    tokens = [t for t in re.split(r"\W+", title.lower()) if len(t) >= 3][:6]
    if not tokens:
        return []
    rows = conn.execute(
        "SELECT node_id, node_kind, title FROM graph_nodes "
        "WHERE status NOT IN ('scaffold', 'stub') AND node_kind IN "
        "('person', 'project', 'organization', 'group')"
    ).fetchall()
    scored: List[Tuple[int, str, str]] = []
    for row in rows:
        label = str(row["title"] or "").lower()
        score = sum(1 for t in tokens if t in label)
        if score:
            scored.append((score, str(row["node_id"]), str(row["node_kind"])))
    scored.sort(key=lambda x: -x[0])
    return [(nid, kind) for _, nid, kind in scored[:3]]


def _extract_emails(payload: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    for key in ("detected_emails", "emails"):
        raw = payload.get(key)
        if isinstance(raw, list):
            found.extend(str(x).strip() for x in raw if x)
    for key in ("excerpt", "ax_text_sample", "text", "page_text"):
        text = str(payload.get(key) or "")
        found.extend(_EMAIL_RE.findall(text))
    return list(dict.fromkeys(e for e in found if "@" in e))[:5]


def _payload_host(payload: Dict[str, Any]) -> str:
    for key in ("url_host", "host", "domain"):
        h = str(payload.get(key) or "").strip().lower()
        if h:
            return h.lstrip("www.")
    url = str(payload.get("url") or payload.get("page_url") or "")
    m = re.search(r"https?://([^/]+)", url, re.I)
    if m:
        return m.group(1).lower().lstrip("www.")
    return ""


def _label_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    local = re.sub(r"[._+\-]+", " ", local).strip()
    if len(local) < 2 or local.isdigit():
        return email.split("@", 1)[0][:80]
    return " ".join(w.capitalize() for w in local.split()[:4])


def _label_from_host(host: str) -> str:
    host = host.lower().strip()
    if not host or host in _GENERIC_HOSTS:
        return ""
    base = host.split(".")[0]
    if base in ("github", "localhost", "notion", "slack"):
        return ""
    return base.replace("-", " ").title()


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip())[:160]


def _skip_title(title: str, app: str) -> bool:
    if not title or len(title) < 3:
        return True
    lower = title.lower()
    if app.lower() in _SKIP_APPS:
        return True
    return any(s in lower for s in _SKIP_TITLE_SUBSTR)


def _looks_like_project(title: str) -> bool:
    if len(title) < 4:
        return False
    if " - " in title or " | " in title or " — " in title:
        left = re.split(r"\s[-|—]\s", title, maxsplit=1)[0].strip()
        return len(left) >= 4 and not _skip_title(left, "")
    words = [w for w in re.split(r"\W+", title) if w]
    return len(words) >= 2 and sum(1 for w in words if w[:1].isupper()) >= 2


def _parse_json_list(raw: Any) -> List[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def _parse_summary(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    text = str(raw).strip()
    if text.startswith("{"):
        try:
            val = json.loads(text)
            return val if isinstance(val, dict) else {"user_note": text}
        except json.JSONDecodeError:
            pass
    return {"user_note": text}


def _node_title(conn, node_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    return str(row["title"]) if row else "node"


def _node_kind(conn, node_id: str) -> str:
    row = conn.execute(
        "SELECT node_kind FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    return str(row["node_kind"]) if row else "person"
