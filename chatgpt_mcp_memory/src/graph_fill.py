"""Graph-fill queue: gaps 42 asks about so the life graph gets filled in."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from chat_store import chat_message_insert, chat_thread_get, chat_thread_insert, chat_thread_resolve
from corpus_context import corpus_summary_line, prefetch_for_subject
from entity_resolution import ensure_person_node, link_belongs_to_scaffold
from store import (
    _new_id,
    graph_candidate_get,
    graph_candidate_resolve,
    identity_claim_list,
    identity_claim_set_status,
)

log = logging.getLogger(__name__)

ME_NODE = "scaffold-me"
_FRIENDS_BUCKET = "scaffold-people-friends"

# Scaffold buckets 42 nudges when empty (parent_id, child node_kind, label for copy).
_BUCKET_GAPS: List[Tuple[str, str, str]] = [
    ("scaffold-people-friends", "person", "a friend"),
    ("scaffold-people-family", "person", "a family member"),
    ("scaffold-projects-active", "project", "something you're actively working on"),
    ("scaffold-places-home", "place", "where you live"),
    ("scaffold-work-companies", "organization", "where you work"),
    ("scaffold-groups-teams", "group", "a team or community you belong to"),
]


def pick_next_gap(conn, data_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Highest-priority unfilled graph slot, or None."""
    try:
        from graph_phantom import purge_phantom_graph_artifacts

        purge_phantom_graph_artifacts(conn, commit=True)
    except Exception:
        log.debug("phantom graph purge skipped", exc_info=True)

    claims = identity_claim_list(conn, status="proposed", limit=1)
    if claims:
        c = claims[0]
        label = (c.get("text") or c.get("kind") or "claim")[:120]
        return {
            "gap_type": "claim",
            "claim_id": c.get("claim_id"),
            "label": label,
            "kind": c.get("kind"),
        }

    sparse_rows = conn.execute(
        "SELECT node_id, title, summary FROM graph_nodes "
        "WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub') "
        "AND (summary IS NULL OR summary='' OR summary='{}') "
        "ORDER BY updated_at ASC LIMIT 8"
    ).fetchall()
    for row in sparse_rows:
        title = str(row["title"] or "Someone")
        if not _is_plausible_graph_title(title, node_kind="person"):
            continue
        return {
            "gap_type": "person",
            "subject_id": str(row["node_id"]),
            "label": title,
            "phase": 0,
        }

    relation_rows = conn.execute(
        "SELECT n.node_id, n.title FROM graph_nodes n "
        "WHERE n.node_kind='person' AND n.status NOT IN ('scaffold', 'stub') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM graph_edges e WHERE e.from_node_id=? AND e.to_node_id=n.node_id "
        "  AND e.rel_kind IN ('knows', 'related_to')"
        ") LIMIT 8",
        (ME_NODE,),
    ).fetchall()
    for row in relation_rows:
        title = str(row["title"] or "Someone")
        if not _is_plausible_graph_title(title, node_kind="person"):
            continue
        return {
            "gap_type": "person_relation",
            "subject_id": str(row["node_id"]),
            "label": title,
            "phase": 0,
        }

    for parent_id, node_kind, hint in _BUCKET_GAPS:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM graph_nodes "
            "WHERE parent_node_id=? AND status NOT IN ('scaffold', 'stub')",
            (parent_id,),
        ).fetchone()
        if n and int(n["c"]) == 0:
            parent = conn.execute(
                "SELECT title FROM graph_nodes WHERE node_id=?", (parent_id,)
            ).fetchone()
            bucket = str(parent["title"] if parent else parent_id)
            return {
                "gap_type": "bucket",
                "parent_node_id": parent_id,
                "node_kind": node_kind,
                "bucket_label": bucket,
                "hint": hint,
            }

    if data_dir:
        cap = _capture_gap(data_dir)
        if cap:
            return cap

    return None


def _capture_gap(data_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        from screen_context_store import current_record

        rec = current_record(data_dir)
        if not rec:
            return None
        app = str(rec.get("app_name") or "").strip()
        title = str(rec.get("window_title") or "").strip()
        if not title and not app:
            return None
        return {
            "gap_type": "capture",
            "app_name": app,
            "window_title": title[:120],
        }
    except Exception:
        log.debug("capture gap skipped", exc_info=True)
        return None


def contact_suggestions_for_gap(
    conn,
    gap: Dict[str, Any],
    data_dir: Optional[Path],
    *,
    limit: int = 5,
) -> List[Dict[str, str]]:
    """Names from Contacts not yet on the graph for this bucket (friends v1)."""
    if not data_dir or gap.get("gap_type") != "bucket":
        return []
    parent_id = str(gap.get("parent_node_id") or "")
    if parent_id != _FRIENDS_BUCKET or str(gap.get("node_kind") or "") != "person":
        return []
    try:
        from life_evidence_snapshot import load_contact_names, refresh_life_evidence_if_stale

        refresh_life_evidence_if_stale(Path(data_dir))
        names = load_contact_names(Path(data_dir))
    except Exception:
        log.debug("contact suggestions skipped", exc_info=True)
        return []
    if not names:
        return []

    existing = _graph_person_titles(conn, parent_id=parent_id)
    out: List[Dict[str, str]] = []
    for name in names:
        if _norm_person_name(name) in existing:
            continue
        out.append({"name": name, "source": "contacts"})
        if len(out) >= limit:
            break
    return out


def apply_graph_candidate_resolution(
    conn,
    candidate_id: str,
    *,
    status: str,
    payload: Optional[Dict[str, Any]] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve graph candidates and apply approved screen entities to the graph."""
    candidate = graph_candidate_get(conn, candidate_id)
    if not candidate:
        return {"ok": False, "error": "candidate_not_found"}
    merge = dict(payload or {})
    deltas: List[str] = []
    node_id = ""

    if status in ("approved", "merged") and candidate.get("candidate_type") == "screen_entity":
        result = _apply_screen_entity_candidate(conn, candidate, merge)
        if not result.get("ok"):
            return result
        node_id = str(result.get("node_id") or "")
        deltas.extend(result.get("deltas") or [])
        merge.update(result.get("payload") or {})
    elif status in ("approved", "merged") and candidate.get("candidate_type") == "person_merge":
        result = _apply_person_merge_candidate(conn, candidate, merge)
        if not result.get("ok"):
            return result
        node_id = str(result.get("node_id") or "")
        deltas.extend(result.get("deltas") or [])
        merge.update(result.get("payload") or {})

    row = graph_candidate_resolve(conn, candidate_id, status=status, payload_merge=merge)
    out = {"ok": True, "candidate": row, "deltas": deltas, "node_id": node_id or None}
    if status in ("approved", "merged") and deltas:
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
    return out


def _apply_screen_entity_candidate(
    conn,
    candidate: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    original = dict(candidate.get("payload") or {})
    merged = {**original, **payload}
    label = _screen_candidate_label(merged)
    if not _is_plausible_graph_title(label, node_kind="person"):
        return {"ok": False, "error": "missing_person_label"}
    meta = {
        "source": "screen_memory",
        "evidence_refs": candidate.get("evidence_refs") or [],
    }
    for key in ("email", "phone", "imessage", "handle", "screen_event_id", "app", "window", "url"):
        if merged.get(key):
            meta[key] = merged[key]
    note = str(
        payload.get("relationship")
        or payload.get("relation_note")
        or payload.get("user_note")
        or payload.get("note")
        or ""
    ).strip()
    if note:
        meta["user_note"] = note[:500]
    node_id = ensure_person_node(conn, label=label, meta=meta)
    link_belongs_to_scaffold(conn, node_id)
    _person_summary(conn, node_id, note or _screen_candidate_summary(merged))
    relation_note = note or str(merged.get("relationship") or "").strip()
    if relation_note:
        _link_knows(conn, ME_NODE, node_id, relation_note)
    resolved_payload = {
        "resolved_node_id": node_id,
        "resolved_label": label,
        "applied_as": "person",
    }
    return {
        "ok": True,
        "node_id": node_id,
        "payload": resolved_payload,
        "deltas": [f"Added **{label}** from screen memory to your graph."],
    }


def _apply_person_merge_candidate(
    conn,
    candidate: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    original = dict(candidate.get("payload") or {})
    merged = {**original, **payload}
    node_id = str(merged.get("existing_node_id") or merged.get("target_node_id") or "").strip()
    if not node_id:
        return {"ok": False, "error": "missing_existing_node_id"}
    row = conn.execute(
        "SELECT node_id, title, aliases_json, summary, source_refs_json, confidence "
        "FROM graph_nodes WHERE node_id=? AND node_kind='person'",
        (node_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": "person_not_found"}

    summary = _json_object(row["summary"] or "")
    incoming_meta = merged.get("incoming_meta") if isinstance(merged.get("incoming_meta"), dict) else {}
    incoming_label = str(merged.get("incoming_label") or "").strip()
    aliases = _json_list(row["aliases_json"] or "[]")
    for value in [
        incoming_label,
        str(incoming_meta.get("email") or ""),
        str(incoming_meta.get("phone") or ""),
        str(incoming_meta.get("handle") or ""),
        str(incoming_meta.get("imessage") or ""),
    ]:
        if value.strip() and value.strip() not in aliases:
            aliases.append(value.strip())

    for key in ("email", "phone", "imessage", "handle", "external_id", "identifier", "source"):
        if incoming_meta.get(key):
            _merge_summary_value(summary, key, incoming_meta[key])
    for key in ("incoming_identifiers", "existing_identifiers"):
        vals = [str(v) for v in merged.get(key) or [] if str(v).strip()]
        if vals:
            _merge_summary_value(summary, key, vals)
    reasoning = [str(r) for r in merged.get("reasoning") or [] if str(r).strip()]
    if reasoning:
        summary["last_merge_reasoning"] = reasoning[:8]
    note = str(
        payload.get("note")
        or payload.get("user_note")
        or payload.get("relationship")
        or ""
    ).strip()
    if note:
        summary["user_note"] = note[:500]
    summary["merged_at"] = time.time()

    refs = _json_list(row["source_refs_json"] or "[]")
    for ref in list(candidate.get("evidence_refs") or []) + _evidence_refs_from_meta(incoming_meta):
        if str(ref).strip() and str(ref).strip() not in refs:
            refs.append(str(ref).strip())
    confidence = max(float(row["confidence"] or 0), float(candidate.get("confidence") or 0), 0.64)
    conn.execute(
        "UPDATE graph_nodes SET aliases_json=?, summary=?, source_refs_json=?, confidence=?, "
        "updated_at=?, status='active' WHERE node_id=?",
        (
            json.dumps(aliases[:40], ensure_ascii=False),
            json.dumps(summary, ensure_ascii=False),
            json.dumps(refs[:80], ensure_ascii=False),
            confidence,
            time.time(),
            node_id,
        ),
    )
    link_belongs_to_scaffold(conn, node_id)
    title = str(row["title"] or incoming_label or "person")
    identifiers = sorted({str(v) for v in merged.get("incoming_identifiers") or [] if str(v).strip()})
    return {
        "ok": True,
        "node_id": node_id,
        "payload": {
            "resolved_node_id": node_id,
            "resolved_label": title,
            "applied_as": "person_merge",
            "merged_identifiers": identifiers,
        },
        "deltas": [f"Merged new identifiers for **{title}** into your graph."],
    }


def _screen_candidate_label(payload: Dict[str, Any]) -> str:
    for key in ("label", "name", "display_name", "person_name"):
        raw = str(payload.get(key) or "").strip()
        if raw:
            return _first_phrase(raw)
    email = str(payload.get("email") or "").strip()
    if email and "@" in email:
        return email.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
    return _first_phrase(str(payload.get("handle") or payload.get("phone") or payload.get("imessage") or ""))


def _screen_candidate_summary(payload: Dict[str, Any]) -> str:
    bits = []
    for key in ("email", "phone", "imessage", "handle"):
        if payload.get(key):
            bits.append(f"{key}: {payload[key]}")
    app = str(payload.get("app") or "").strip()
    window = str(payload.get("window") or "").strip()
    if app or window:
        bits.append(f"seen in {app}: {window}".strip(": "))
    return "; ".join(bits)[:500]


def _json_object(raw: str) -> Dict[str, Any]:
    if not raw or not str(raw).strip().startswith("{"):
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _json_list(raw: str) -> List[str]:
    try:
        vals = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(vals, list):
        return []
    return [str(v) for v in vals if str(v).strip()]


def _merge_summary_value(summary: Dict[str, Any], key: str, value: Any) -> None:
    vals = value if isinstance(value, list) else [value]
    clean = [str(v).strip() for v in vals if str(v).strip()]
    if not clean:
        return
    existing = summary.get(key)
    if existing is None or existing == "":
        summary[key] = clean if len(clean) > 1 else clean[0]
        return
    merged = existing if isinstance(existing, list) else [existing]
    merged_clean = [str(v).strip() for v in merged if str(v).strip()]
    for val in clean:
        if val not in merged_clean:
            merged_clean.append(val)
    summary[key] = merged_clean if len(merged_clean) > 1 else merged_clean[0]


def _evidence_refs_from_meta(meta: Dict[str, Any]) -> List[str]:
    refs = meta.get("evidence_refs") or meta.get("source_refs") or []
    if isinstance(refs, list):
        return [str(r) for r in refs if str(r).strip()][:8]
    if isinstance(refs, str) and refs.strip():
        return [refs.strip()]
    return []


def compose_question(conn, gap: Dict[str, Any], *, data_dir: Optional[Path] = None) -> str:
    """42-voiced question for a gap."""
    gtype = gap.get("gap_type")
    if gtype == "claim":
        label = gap.get("label") or "this"
        corpus = prefetch_for_subject(conn, subject_label=str(label), top_k=3)
        body = (
            f"**42:** Mirror flagged **{gap.get('kind', 'identity')}** — should this land on your graph?\n\n"
            f"> {label}\n\n"
        )
        cite = corpus_summary_line(corpus)
        if cite:
            body += f"{cite}\n\n"
        body += "Reply **yes** to add it, **no** to drop it, or correct me in a sentence."
        return body

    if gtype == "person":
        label = gap.get("label") or "someone"
        sid = gap.get("subject_id")
        corpus = prefetch_for_subject(
            conn, subject_label=str(label), subject_id=sid, top_k=3
        )
        body = f"**42:** Who is **{label}** to you? One line is enough — I'll file it on your graph."
        cite = corpus_summary_line(corpus)
        if cite:
            body += f"\n\n{cite}"
        return body

    if gtype == "person_relation":
        label = gap.get("label") or "someone"
        return (
            f"**42:** How do you know **{label}**? "
            "(e.g. coworker, cousin, neighbor — I'll link them to you on the graph.)"
        )

    if gtype == "bucket":
        bucket = gap.get("bucket_label") or "this section"
        hint = gap.get("hint") or "someone or something"
        suggestions = gap.get("suggestions") or contact_suggestions_for_gap(conn, gap, data_dir)
        if suggestions:
            gap["suggestions"] = suggestions
            labels = ", ".join(f"**{s['name']}**" for s in suggestions[:5])
            body = (
                f"**42:** Your **{bucket}** bucket is empty. "
                f"From Contacts I have {labels} not on your graph yet — "
                "tap a name below or type one; I'll add them and link you."
            )
            cite = _ambient_corpus_line(conn, "friends contacts people")
            if cite:
                body += f"\n\n{cite}"
            return body
        body = (
            f"**42:** Your **{bucket}** bucket is empty. "
            f"Name {hint} — I'll add a node and wire it into the graph."
        )
        cite = _ambient_corpus_line(conn, f"{bucket} {hint}")
        if cite:
            body += f"\n\n{cite}"
        return body

    if gtype == "capture":
        app = gap.get("app_name") or "your Mac"
        title = gap.get("window_title") or ""
        line = f"**{app}**" + (f" — “{title}”" if title else "")
        return (
            f"**42:** I noticed {line}. "
            "Who or what is this on your graph — a **person**, **project**, or skip?"
        )

    return "**42:** What's one person or project I should add to your graph right now?"


def open_thread_for_gap(conn, gap: Dict[str, Any], *, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Create thread + first assistant message for a gap."""
    from forty_two_infer import try_fill_gap_from_corpus

    auto_attempts = 0
    while gap and auto_attempts < 8:
        infer = try_fill_gap_from_corpus(conn, gap, data_dir=data_dir)
        if infer.get("status") == "filled" and infer.get("gap_exhausted"):
            auto_attempts += 1
            nxt = pick_next_gap(conn, data_dir)
            if not nxt:
                return {
                    "thread": None,
                    "created": False,
                    "auto_filled": True,
                    "deltas": infer.get("deltas") or [],
                    "message": "Filled graph gaps from your saved context.",
                }
            gap = nxt
            continue
        if infer.get("status") == "needs_question":
            gap = dict(gap)
            gap["infer_question"] = infer.get("ambiguous_question")
            gap["evidence_cite"] = infer.get("evidence_cite")
        break

    meta: Dict[str, Any] = {"mode": "forty_two", "gap": gap}
    if gap.get("claim_id"):
        meta["claim_id"] = gap["claim_id"]
    topic = "42 · graph"
    subject_id = gap.get("subject_id")
    if gap.get("gap_type") == "claim":
        topic = f"42 · {gap.get('kind', 'mirror')}"
    elif gap.get("gap_type") == "person":
        topic = f"42 · {gap.get('label', 'person')}"
    elif gap.get("gap_type") == "bucket":
        topic = f"42 · {gap.get('bucket_label', 'graph')}"

    tid = chat_thread_insert(
        conn,
        subject_id=subject_id,
        topic=topic,
        meta=meta,
    )
    suggestions = contact_suggestions_for_gap(conn, gap, data_dir) if gap.get("gap_type") == "bucket" else []
    if suggestions:
        gap["suggestions"] = suggestions
    body = compose_question(conn, gap, data_dir=data_dir)
    if gap.get("infer_question"):
        cite = gap.get("evidence_cite") or ""
        body = f"**42:** {gap['infer_question']}"
        if cite:
            body += f"\n\nFrom your notes — {cite}"
    else:
        try:
            from forty_two_llm import compose_opening_question

            llm_body, used = compose_opening_question(conn, gap, data_dir=data_dir)
            if used and llm_body:
                body = llm_body
        except Exception:
            log.debug("42 llm opening skipped", exc_info=True)
    msg_meta: Dict[str, Any] = {"gap": gap, "speaker": "42"}
    if gap.get("suggestions"):
        msg_meta["suggestions"] = gap["suggestions"]
    chat_message_insert(
        conn,
        thread_id=tid,
        role="assistant",
        body_md=body,
        meta=msg_meta,
    )
    return {"thread": chat_thread_get(conn, tid), "created": True, "gap": gap}


def apply_answer(
    conn,
    thread: Dict[str, Any],
    *,
    body: str,
    action: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write user answer into the graph; return confirmation lines for 42."""
    text = (body or "").strip()
    meta = dict(thread.get("meta") or {})
    gap = dict(meta.get("gap") or {})
    gtype = gap.get("gap_type") or meta.get("gap_type")

    if meta.get("claim_id") and not gap:
        gap = {"gap_type": "claim", "claim_id": meta.get("claim_id"), "label": meta.get("label")}

    deltas: List[str] = []

    claim_id = meta.get("claim_id") or gap.get("claim_id")
    if claim_id and (gtype == "claim" or not gtype):
        reject = action == "reject" or (
            text and text.lower().startswith(("no", "reject"))
        )
        if reject:
            identity_claim_set_status(conn, claim_id, "rejected")
            deltas.append("Rejected that mirror claim — it won't be added to your graph.")
        else:
            identity_claim_set_status(conn, claim_id, "active")
            deltas.append("Approved — claim is on your graph.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True}
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
        return out

    if not text and action != "dismiss":
        return {"ok": False, "error": "empty_message"}

    if action == "dismiss" or text.lower() in ("skip", "later", "not now"):
        chat_thread_resolve(conn, thread["thread_id"])
        return {
            "ok": True,
            "deltas": ["Skipped for now — I'll ask about another gap later."],
            "resolved": True,
        }

    subject_id = thread.get("subject_id") or meta.get("subject_id") or gap.get("subject_id")

    if gtype == "person" and subject_id:
        _person_summary(conn, subject_id, text)
        deltas.append(f"Saved who **{_node_title(conn, subject_id)}** is to you on the graph.")
        gap["gap_type"] = "person_relation"
        gap["subject_id"] = subject_id
        gap["label"] = _node_title(conn, subject_id)
        gap["phase"] = 1
        meta["gap"] = gap
        conn.execute(
            "UPDATE chat_threads SET meta_json=? WHERE thread_id=?",
            (json.dumps(meta, ensure_ascii=False), thread["thread_id"]),
        )
        out = {
            "ok": True,
            "deltas": deltas,
            "resolved": False,
            "follow_up": compose_question(conn, gap),
        }
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
        return out

    if gtype == "person_relation" and subject_id:
        _link_knows(conn, ME_NODE, subject_id, text)
        deltas.append(f"Linked you → **{_node_title(conn, subject_id)}** (`knows`).")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True}
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
        return out

    if gtype == "bucket":
        parent_id = str(gap.get("parent_node_id") or "scaffold-people-friends")
        node_kind = str(gap.get("node_kind") or "person")
        title = _first_phrase(text)
        if not _is_plausible_graph_title(text, node_kind=node_kind):
            bucket = gap.get("bucket_label") or "this section"
            hint = gap.get("hint") or "someone"
            out = {
                "ok": True,
                "deltas": [
                    f"That doesn't look like a name — who should I add under **{bucket}**? "
                    f"(Name {hint}, or tap a contact chip.)"
                ],
                "resolved": False,
                "follow_up": compose_question(conn, gap, data_dir=data_dir),
            }
            return out
        if node_kind == "person" and parent_id == "scaffold-people-friends":
            nid = ensure_person_node(
                conn,
                label=title,
                meta={"user_note": text[:500], "source": "forty_two"},
            )
        else:
            nid = _create_graph_node(conn, parent_id, node_kind, title, user_note=text)
            if node_kind == "person":
                link_belongs_to_scaffold(conn, nid, _people_root(parent_id))
                _link_knows(conn, ME_NODE, nid, text)
        deltas.append(f"Added **{title}** under **{gap.get('bucket_label', 'graph')}**.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True, "node_id": nid}
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
        return out

    if gtype == "capture":
        parsed = _parse_capture_answer(text)
        if parsed.get("skip"):
            deltas.append("Got it — left off the graph for now.")
            chat_thread_resolve(conn, thread["thread_id"])
            return {"ok": True, "deltas": deltas, "resolved": True}
        kind = parsed.get("node_kind") or "person"
        title = parsed.get("title") or _first_phrase(text)
        if not _is_plausible_graph_title(title, node_kind=kind):
            out = {
                "ok": True,
                "deltas": [
                    "That doesn't look like a person or project name — "
                    "say who/what this is, or reply **skip**."
                ],
                "resolved": False,
                "follow_up": compose_question(conn, gap, data_dir=data_dir),
            }
            return out
        parent = _default_parent_for_kind(kind)
        nid = _create_graph_node(conn, parent, kind, title, user_note=text)
        if kind == "person":
            link_belongs_to_scaffold(conn, nid, "scaffold-people")
            _link_knows(conn, ME_NODE, nid, text)
        deltas.append(f"Added **{title}** ({kind}) from what you were looking at.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True, "node_id": nid}
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
        return out

    if subject_id and not gtype:
        _person_summary(conn, subject_id, text)
        deltas.append(f"Saved who **{_node_title(conn, subject_id)}** is on your graph.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True}
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
        return out

    if subject_id:
        _person_summary(conn, subject_id, text)
        deltas.append("Updated your graph.")
        chat_thread_resolve(conn, thread["thread_id"])
        out = {"ok": True, "deltas": deltas, "resolved": True}
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
        return out

    chat_thread_resolve(conn, thread["thread_id"])
    return {"ok": True, "deltas": deltas or ["Noted."], "resolved": True}


def persist_graph_snapshot(conn, data_dir: Optional[Path]) -> None:
    """Best-effort JSON backup of user graph rows (survives mistaken DB rotate)."""
    if not data_dir:
        return
    try:
        nodes = [
            dict(r)
            for r in conn.execute(
                "SELECT node_id, node_kind, title, status, summary, parent_node_id, "
                "aliases_json, confidence, updated_at FROM graph_nodes "
                "WHERE status NOT IN ('scaffold', 'stub')"
            ).fetchall()
        ]
        edges = [
            dict(r)
            for r in conn.execute(
                "SELECT edge_id, from_node_id, to_node_id, rel_kind, created_at, updated_at "
                "FROM graph_edges"
            ).fetchall()
        ]
        path = Path(data_dir) / "graph_snapshot.json"
        path.write_text(
            json.dumps(
                {"saved_at": time.time(), "nodes": nodes, "edges": edges},
                ensure_ascii=False,
                indent=0,
            ),
            encoding="utf-8",
        )
    except Exception:
        log.warning("graph_snapshot write failed", exc_info=True)


def _log_deltas(data_dir: Optional[Path], result: Dict[str, Any]) -> None:
    if not data_dir:
        return
    try:
        from graph_events import log_graph_event

        import re

        for d in result.get("deltas") or []:
            plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", str(d))
            log_graph_event(data_dir, plain, action="forty_two")
    except Exception:
        log.debug("graph event log failed", exc_info=True)


def format_confirmation(result: Dict[str, Any]) -> str:
    lines = [f"**42:** {d}" for d in result.get("deltas") or ["Updated your graph."]]
    follow = result.get("follow_up")
    if follow and not result.get("resolved"):
        lines.append("")
        lines.append(follow)
    elif result.get("resolved"):
        nxt = "Ask me again when you're ready — I'll find the next empty spot."
        lines.append("")
        lines.append(f"**42:** {nxt}")
    return "\n".join(lines)


def _person_summary(conn, node_id: str, text: str) -> None:
    row = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    existing: Dict[str, Any] = {}
    if row and row["summary"] and str(row["summary"]).strip().startswith("{"):
        try:
            existing = json.loads(row["summary"])
        except json.JSONDecodeError:
            pass
    existing["clarified_at"] = time.time()
    existing["user_note"] = text[:500]
    conn.execute(
        "UPDATE graph_nodes SET summary=?, updated_at=?, status='active' WHERE node_id=?",
        (json.dumps(existing, ensure_ascii=False), time.time(), node_id),
    )


def _link_knows(conn, from_id: str, to_id: str, note: str) -> None:
    if not from_id or not to_id or from_id == to_id:
        return
    exists = conn.execute(
        "SELECT 1 FROM graph_edges WHERE from_node_id=? AND to_node_id=? AND rel_kind='knows'",
        (from_id, to_id),
    ).fetchone()
    if exists:
        return
    eid = _new_id("ge")
    now = time.time()
    edge_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(graph_edges)").fetchall()}
    if "updated_at" in edge_cols:
        conn.execute(
            "INSERT INTO graph_edges(edge_id, from_node_id, to_node_id, rel_kind, created_at, updated_at) "
            "VALUES(?, ?, ?, 'knows', ?, ?)",
            (eid, from_id, to_id, now, now),
        )
    else:
        conn.execute(
            "INSERT INTO graph_edges(edge_id, from_node_id, to_node_id, rel_kind, created_at) "
            "VALUES(?, ?, ?, 'knows', ?)",
            (eid, from_id, to_id, now),
        )
    if note:
        row = conn.execute(
            "SELECT summary FROM graph_nodes WHERE node_id=?", (to_id,)
        ).fetchone()
        meta: Dict[str, Any] = {}
        if row and row["summary"] and str(row["summary"]).strip().startswith("{"):
            try:
                meta = json.loads(row["summary"])
            except json.JSONDecodeError:
                pass
        meta["relation_note"] = note[:200]
        conn.execute(
            "UPDATE graph_nodes SET summary=?, updated_at=? WHERE node_id=?",
            (json.dumps(meta, ensure_ascii=False), time.time(), to_id),
        )


def _create_graph_node(
    conn, parent_id: str, node_kind: str, title: str, *, user_note: str = ""
) -> str:
    if node_kind == "person":
        return ensure_person_node(
            conn,
            label=title,
            meta={"user_note": user_note[:500], "source": "forty_two"},
        )
    nid = _new_id("gn")
    meta = {"user_note": user_note[:500], "source": "forty_two"} if user_note else {}
    now = time.time()
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, ?, ?, 'active', '', NULL, ?, '[]', ?, 0.6, '[]', "
        "'vault_local', ?, ?)",
        (
            nid,
            node_kind,
            title[:200],
            parent_id,
            json.dumps(meta, ensure_ascii=False) if meta else "",
            now,
            now,
        ),
    )
    return nid


def _default_parent_for_kind(node_kind: str) -> str:
    return {
        "person": "scaffold-people-friends",
        "project": "scaffold-projects-active",
        "place": "scaffold-places-frequent",
        "organization": "scaffold-work-companies",
        "group": "scaffold-groups-teams",
    }.get(node_kind, "scaffold-me")


def _people_root(parent_id: str) -> str:
    if parent_id.startswith("scaffold-people"):
        return "scaffold-people"
    return parent_id


def _node_title(conn, node_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    return str(row["title"]) if row else "them"


def _norm_person_name(name: str) -> str:
    return " ".join((name or "").lower().split())


def _graph_person_titles(conn, *, parent_id: Optional[str] = None) -> set[str]:
    """Normalized person titles already on the graph (optionally in one bucket)."""
    if parent_id:
        rows = conn.execute(
            "SELECT title FROM graph_nodes WHERE node_kind='person' "
            "AND parent_node_id=? AND status NOT IN ('scaffold', 'stub')",
            (parent_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT title FROM graph_nodes WHERE node_kind='person' "
            "AND status NOT IN ('scaffold', 'stub')"
        ).fetchall()
    return {_norm_person_name(str(r["title"])) for r in rows if r["title"]}


def _ambient_corpus_line(conn, query: str) -> str:
    try:
        from corpus_context import corpus_summary_line, prefetch_for_subject

        return corpus_summary_line(prefetch_for_subject(conn, subject_label=query, top_k=2))
    except Exception:
        return ""


_FILLER_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "hi",
        "hey",
        "hello",
        "hiya",
        "howdy",
        "yo",
        "sup",
        "there",
        "here",
        "thanks",
        "thank",
        "thx",
        "ty",
        "ok",
        "okay",
        "sure",
        "yes",
        "no",
        "nope",
        "yep",
        "yeah",
        "nah",
        "lol",
        "haha",
        "test",
        "testing",
        "what",
        "why",
        "who",
        "when",
        "where",
        "how",
        "please",
        "sorry",
        "idk",
        "dunno",
    }
)

_PHRASE_FILLER = frozenset(
    {
        "hi there",
        "hey there",
        "hello there",
        "good morning",
        "good afternoon",
        "good evening",
        "thank you",
        "thanks so much",
        "not sure",
        "i dont know",
        "i don't know",
        "no idea",
    }
)


def _is_plausible_graph_title(text: str, *, node_kind: str = "person") -> bool:
    """Reject greetings and filler when 42 expects a real graph label."""
    phrase = _first_phrase(text).strip()
    if len(phrase) < 2:
        return False
    low = " ".join(phrase.lower().split())
    if low in _PHRASE_FILLER:
        return False
    tokens = low.split()
    if all(t in _FILLER_WORDS for t in tokens):
        return False
    if node_kind == "person":
        nameish = [t for t in tokens if len(t) >= 2 and t not in _FILLER_WORDS]
        if not nameish:
            return False
    return True


def _first_phrase(text: str) -> str:
    t = text.strip()
    for sep in (".", ",", "—", "-", "–"):
        if sep in t:
            t = t.split(sep)[0]
    return (t.strip() or "Untitled")[:200]


def _parse_capture_answer(text: str) -> Dict[str, Any]:
    low = text.lower().strip()
    if low in ("skip", "no", "nothing", "n/a"):
        return {"skip": True}
    if "project" in low:
        title = text.replace("project", "").replace("Project", "").strip(" :—-") or _first_phrase(text)
        return {"node_kind": "project", "title": _first_phrase(title)}
    if "place" in low:
        return {"node_kind": "place", "title": _first_phrase(text)}
    if "org" in low or "company" in low or "work" in low:
        return {"node_kind": "organization", "title": _first_phrase(text)}
    return {"node_kind": "person", "title": _first_phrase(text)}
