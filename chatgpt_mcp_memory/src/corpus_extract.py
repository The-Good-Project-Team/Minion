"""L3: ingest-time entity extraction → confirmable graph candidates.

When a corpus is ingested, read its chunks, ask the LLM which real-world
entities appear (people other than the user, organizations, projects), and file
each as a `corpus_entity` graph candidate the user can confirm — e.g. "Is this
your company? How are you connected?". Confirmation routes through minion's
existing candidate/clarification system rather than auto-committing.

Corpus-agnostic by construction: nothing is hardcoded to any person or site —
entities are whatever the ingested text yields, for any future user.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from store import graph_candidate_create, graph_candidate_list

log = logging.getLogger(__name__)

# LLM-extracted kinds mapped onto the graph's allowed node kinds.
_KIND_MAP = {
    "person": "person",
    "people": "person",
    "organization": "organization",
    "organisation": "organization",
    "company": "organization",
    "org": "organization",
    "project": "project",
    "product": "project",
    "place": "place",
    "location": "place",
    "group": "group",
    "team": "group",
}
_DEFAULT_CONFIDENCE = 0.5
_CANDIDATE_TYPE = "corpus_entity"

_SYSTEM = (
    "You build a personal knowledge graph for ONE user from their own documents, "
    "conversations, and notes. First understand the material, then extract the "
    "distinct real-world entities that matter: people (other than the user), "
    "organizations, projects/products, places, and groups.\n"
    "For EACH entity return:\n"
    "  - label: the proper name\n"
    "  - kind: one of person, organization, project, place, group\n"
    "  - evidence: a short phrase quoting/paraphrasing what the text says about it\n"
    "  - relation_hypothesis: your best guess of how this entity relates to the "
    "user or to other entities, based ONLY on the evidence (e.g. 'appears to be "
    "the user's employer', 'a colleague mentioned alongside project X', "
    "'unclear — only named once'). Say 'unknown' if the text gives no signal.\n"
    "  - question: ONE genuine, specific question whose answer would resolve the "
    "single biggest uncertainty about this entity and its place in the user's "
    "life. Ground it in the evidence; reference what you actually saw. Do NOT use "
    "a generic template — ask what truly needs clarifying for THIS entity. If the "
    "relation is already obvious from the text, ask to confirm and deepen it "
    "rather than asking the obvious.\n"
    "Rules: do NOT invent entities unsupported by the text; do NOT include the "
    "user themselves; merge obvious duplicates; prefer proper names; skip pure "
    "tooling/library noise. Return strict JSON."
)

_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "kind": {"type": "string"},
                    "evidence": {"type": "string"},
                    "relation_hypothesis": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["label", "kind", "question"],
            },
        }
    },
    "required": ["entities"],
}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _map_kind(raw: str) -> Optional[str]:
    return _KIND_MAP.get((raw or "").strip().lower())


def _collect_evidence(
    conn,
    source_ids: Optional[List[str]],
    *,
    max_chars: int,
) -> tuple[str, List[str]]:
    """Concatenate chunk text (and gather source paths) for the given sources.

    With no source_ids, samples the most recently ingested sources.
    """
    if source_ids:
        placeholders = ",".join("?" * len(source_ids))
        rows = conn.execute(
            f"SELECT c.text, s.path FROM chunks c JOIN sources s ON s.source_id = c.source_id "
            f"WHERE c.source_id IN ({placeholders}) ORDER BY c.source_id, c.seq",
            tuple(source_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT c.text, s.path FROM chunks c JOIN sources s ON s.source_id = c.source_id "
            "ORDER BY s.updated_at DESC, c.seq LIMIT 400"
        ).fetchall()
    parts: List[str] = []
    paths: List[str] = []
    total = 0
    seen_paths: set[str] = set()
    for r in rows:
        text = str(r["text"] or "").strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            text = text[: max(0, max_chars - total)]
        parts.append(text)
        total += len(text)
        p = str(r["path"] or "")
        if p and p not in seen_paths:
            seen_paths.add(p)
            paths.append(p)
        if total >= max_chars:
            break
    return "\n\n---\n\n".join(parts), paths


def _existing_labels(conn) -> set[str]:
    """Normalized labels already on the graph or already pending as candidates."""
    out: set[str] = set()
    for row in conn.execute(
        "SELECT title FROM graph_nodes WHERE status NOT IN ('scaffold', 'stub')"
    ).fetchall():
        out.add(_norm(str(row["title"] or "")))
    for cand in graph_candidate_list(conn, status="open", limit=200):
        lbl = (cand.get("payload") or {}).get("label")
        if lbl:
            out.add(_norm(str(lbl)))
    out.discard("")
    return out


_SHAPE_SYSTEM = (
    "You are looking at a fresh sample of material a user just added to their "
    "personal knowledge system. Before anything is extracted, describe what this "
    "collection appears to BE. In 2-4 sentences: what is it, who or what does it "
    "center on, and what kinds of entities and relationships dominate (people, "
    "organizations, projects, places, events, ideas)? Be concrete and grounded in "
    "the sample; do not guess beyond it. Return strict JSON: "
    '{"shape": "<the description>", "dominant_kinds": ["..."]}'
)

_SHAPE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "shape": {"type": "string"},
        "dominant_kinds": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["shape"],
}


def characterize_corpus(
    conn,
    data_dir: Optional[Path],
    source_ids: Optional[List[str]] = None,
    *,
    max_chars: int = 8000,
) -> Dict[str, Any]:
    """Snapshot the data and ask the LLM what it looks like — before any graphing."""
    if not _gemini_ok(data_dir):
        return {"shape": "", "dominant_kinds": []}
    evidence, _ = _collect_evidence(conn, source_ids, max_chars=max_chars)
    if not evidence.strip():
        return {"shape": "", "dominant_kinds": []}
    try:
        from gemini_client import gemini_chat, graph_mine_gemini_model

        raw = gemini_chat(
            system=_SHAPE_SYSTEM,
            messages=[{"role": "user", "content": f"SAMPLE:\n{evidence}"}],
            data_dir=data_dir,
            model=graph_mine_gemini_model(data_dir) if data_dir else None,
            temperature=0.2,
            max_output_tokens=512,
            response_mime_type="application/json",
            response_schema=_SHAPE_SCHEMA,
        )
        data = json.loads(raw)
    except Exception as exc:
        log.warning("corpus characterize failed: %s", exc)
        return {"shape": "", "dominant_kinds": []}
    return {
        "shape": str(data.get("shape") or "").strip(),
        "dominant_kinds": [str(k) for k in (data.get("dominant_kinds") or []) if str(k).strip()],
    }


def _extract(
    evidence: str,
    data_dir: Optional[Path],
    *,
    max_entities: int,
    shape: str = "",
) -> List[Dict[str, Any]]:
    from gemini_client import gemini_chat, graph_mine_gemini_model

    shape_line = (
        f"This collection appears to be: {shape}\nUse that understanding to ground "
        f"your questions in what this material actually is.\n\n"
        if shape
        else ""
    )
    user = (
        f"{shape_line}"
        f"Extract up to {max_entities} distinct entities from the text below. "
        f"Return JSON matching the schema.\n\nTEXT:\n{evidence}"
    )
    raw = gemini_chat(
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
        data_dir=data_dir,
        model=graph_mine_gemini_model(data_dir) if data_dir else None,
        temperature=0.2,
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Tolerate a fenced or chatty wrapper around the JSON.
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            log.warning("corpus extract: unparseable LLM output: %r", raw[:200])
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    ents = data.get("entities") if isinstance(data, dict) else None
    return ents if isinstance(ents, list) else []


def extract_entities_for_sources(
    conn,
    data_dir: Optional[Path],
    source_ids: Optional[List[str]] = None,
    *,
    max_chars: int = 12000,
    max_entities: int = 15,
) -> Dict[str, Any]:
    """Extract entities from the given (or most recent) sources → corpus_entity candidates."""
    try:
        from gemini_client import gemini_configured

        if not gemini_configured(data_dir):
            return {"status": "no_gemini", "created": 0, "entities": []}
    except Exception:
        return {"status": "no_gemini", "created": 0, "entities": []}

    evidence, paths = _collect_evidence(conn, source_ids, max_chars=max_chars)
    if not evidence.strip():
        return {"status": "no_evidence", "created": 0, "entities": []}

    # Step 1: ask the LLM what this material looks like, before graphing anything.
    shape_info = characterize_corpus(conn, data_dir, source_ids)
    shape = shape_info.get("shape", "")

    # Step 2: extract entities + grounded questions, informed by the shape.
    try:
        raw_entities = _extract(evidence, data_dir, max_entities=max_entities, shape=shape)
    except Exception as exc:
        log.warning("corpus entity extraction failed: %s", exc)
        return {"status": "llm_error", "created": 0, "entities": [], "shape": shape}

    existing = _existing_labels(conn)
    evidence_refs = [f"source_path:{p}" for p in paths[:10]]
    created: List[Dict[str, str]] = []
    seen: set[str] = set()
    for ent in raw_entities:
        if not isinstance(ent, dict):
            continue
        label = str(ent.get("label") or "").strip()
        kind = _map_kind(str(ent.get("kind") or ""))
        if not label or not kind:
            continue
        key = _norm(label)
        if key in existing or key in seen or len(key) < 2:
            continue
        note = str(ent.get("evidence") or "").strip()[:400]
        relation = str(ent.get("relation_hypothesis") or "").strip()[:300]
        llm_q = str(ent.get("question") or "").strip()
        # Zero hardcoded questions: if the LLM gave no grounded question, skip the
        # entity rather than fabricate a template prompt.
        if not llm_q:
            continue
        seen.add(key)
        body = f"**Librarian:** {llm_q}"
        graph_candidate_create(
            conn,
            candidate_type=_CANDIDATE_TYPE,
            title=label,
            body_md=body,
            payload={
                "label": label,
                "node_kind": kind,
                "evidence": note,
                "relation_hypothesis": relation,
                "question": llm_q,
                "import_policy": "candidates_only",
            },
            evidence_refs=evidence_refs,
            confidence=_DEFAULT_CONFIDENCE,
            source="corpus_extract",
        )
        created.append({"label": label, "kind": kind})
    if created:
        conn.commit()
        try:
            from graph_events import log_graph_event

            names = ", ".join(c["label"] for c in created[:3])
            log_graph_event(
                data_dir,
                f"**Librarian** found {len(created)} thing(s) to confirm from new context: {names}.",
                action="extract",
            )
        except Exception:
            log.debug("corpus extract event log failed", exc_info=True)
    return {
        "status": "ok",
        "created": len(created),
        "entities": created,
        "shape": shape,
        "dominant_kinds": shape_info.get("dominant_kinds", []),
    }


_APPLY_SYSTEM = (
    "You maintain a personal knowledge graph for ONE user. The user just answered a "
    "question about an entity. Use their answer PLUS the prior evidence to extend the "
    "graph. Return strict JSON:\n"
    "  - summary: one-paragraph factual summary of the entity from the user's point of "
    "view (string; '' if nothing to add)\n"
    "  - relations: array of {target_label, target_kind, rel_kind, note} for connections "
    "the answer reveals. Use target_label 'me' for the user. rel_kind ∈ {knows, "
    "related_to, works_at, belongs_to, participates_in, owns, manages, founded, lives_at, "
    "married_to, parent_of, child_of}. target_kind ∈ {person, organization, project, "
    "place, group}.\n"
    "  - follow_ups: array of {label, kind, question} — at most 3 NEW entities or open "
    "threads the answer genuinely surfaced and worth confirming next. Each question must "
    "be specific and grounded in the answer. Empty array if there are no real follow-ups.\n"
    "Base everything strictly on the answer and evidence; never invent."
)

_APPLY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_label": {"type": "string"},
                    "target_kind": {"type": "string"},
                    "rel_kind": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["target_label", "rel_kind"],
            },
        },
        "follow_ups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "kind": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["label", "question"],
            },
        },
    },
    "required": ["summary", "relations", "follow_ups"],
}


def _resolve_or_create_node(conn, label: str, kind: str) -> Optional[str]:
    """Find an existing non-scaffold node by label, else create one."""
    from graph_fill import ME_NODE, _create_graph_node, _default_parent_for_kind

    norm = _norm(label)
    if norm in ("me", "i", "myself", "the user", "user"):
        return ME_NODE
    row = conn.execute(
        "SELECT node_id FROM graph_nodes WHERE lower(title)=? "
        "AND status NOT IN ('scaffold', 'stub') LIMIT 1",
        (norm,),
    ).fetchone()
    if row:
        return str(row["node_id"])
    k = kind if kind in ("person", "organization", "project", "place", "group") else "person"
    return _create_graph_node(conn, _default_parent_for_kind(k), k, label.strip()[:200])


def apply_answer(
    conn,
    data_dir: Optional[Path],
    candidate: Dict[str, Any],
    answer: str,
    *,
    node_id: str,
) -> Dict[str, Any]:
    """Turn the user's free-text answer into graph mutations + follow-up questions.

    Updates the confirmed node's summary, creates the relations the answer reveals
    (to the user or to other entities, creating those nodes as needed), and files
    genuine follow-up questions as new candidates. Best-effort: returns empty on
    any failure so confirmation never hard-fails.
    """
    answer = (answer or "").strip()
    if not answer:
        return {"deltas": [], "follow_ups": []}
    try:
        from gemini_client import gemini_chat, graph_mine_gemini_model

        if not _gemini_ok(data_dir):
            return {"deltas": [], "follow_ups": []}
    except Exception:
        return {"deltas": [], "follow_ups": []}

    payload = candidate.get("payload") or {}
    label = str(payload.get("label") or candidate.get("title") or "").strip()
    kind = str(payload.get("node_kind") or "person")
    evidence = str(payload.get("evidence") or "")
    refs = [str(r) for r in (candidate.get("evidence_refs") or []) if str(r).strip()]

    user_msg = (
        f"Entity: {label} (kind: {kind})\n"
        f"Prior evidence: {evidence}\n"
        f"Earlier relation hypothesis: {payload.get('relation_hypothesis') or 'unknown'}\n\n"
        f"The user's answer to your question:\n{answer}\n\n"
        f"Return JSON per the schema."
    )
    try:
        raw = gemini_chat(
            system=_APPLY_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            data_dir=data_dir,
            model=graph_mine_gemini_model(data_dir) if data_dir else None,
            temperature=0.2,
            max_output_tokens=1024,
            response_mime_type="application/json",
            response_schema=_APPLY_SCHEMA,
        )
        data = json.loads(raw)
    except Exception as exc:
        log.warning("corpus answer apply failed for %s: %s", label, exc)
        return {"deltas": [], "follow_ups": []}

    deltas: List[str] = []

    # 1) Refresh the entity's summary from the user's own words.
    summary = str(data.get("summary") or "").strip()
    if summary:
        conn.execute(
            "UPDATE graph_nodes SET summary=?, updated_at=? WHERE node_id=?",
            (json.dumps({"user_note": summary, "source": "corpus_confirm"}, ensure_ascii=False),
             time.time(), node_id),
        )

    # 2) Materialize the relations the answer revealed.
    from librarian_infer import ALLOWED_REL_KINDS, _insert_edge, _normalize_rel_kind

    for rel in data.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        tgt_label = str(rel.get("target_label") or "").strip()
        if not tgt_label or _norm(tgt_label) == _norm(label):
            continue
        rel_kind = _normalize_rel_kind(str(rel.get("rel_kind") or "related_to"))
        if rel_kind not in ALLOWED_REL_KINDS:
            rel_kind = "related_to"
        tgt_kind = _map_kind(str(rel.get("target_kind") or "")) or "person"
        tgt_id = _resolve_or_create_node(conn, tgt_label, tgt_kind)
        if not tgt_id or tgt_id == node_id:
            continue
        _insert_edge(conn, node_id, tgt_id, rel_kind, refs, 0.78)
        deltas.append(f"Linked **{label}** —{rel_kind}→ **{tgt_label}**.")

    # 3) Spawn genuine follow-up questions as new candidates.
    existing = _existing_labels(conn)
    follow_ups: List[Dict[str, str]] = []
    for fu in (data.get("follow_ups") or [])[:3]:
        if not isinstance(fu, dict):
            continue
        fl = str(fu.get("label") or "").strip()
        fk = _map_kind(str(fu.get("kind") or "")) or "person"
        fq = str(fu.get("question") or "").strip()
        if not fl or not fq or _norm(fl) in existing:
            continue
        existing.add(_norm(fl))
        graph_candidate_create(
            conn,
            candidate_type=_CANDIDATE_TYPE,
            title=fl,
            body_md=f"**Librarian:** {fq}",
            payload={
                "label": fl,
                "node_kind": fk,
                "question": fq,
                "spawned_from": node_id,
                "import_policy": "candidates_only",
            },
            evidence_refs=refs,
            confidence=_DEFAULT_CONFIDENCE,
            source="corpus_followup",
        )
        follow_ups.append({"label": fl, "kind": fk, "question": fq})

    if deltas or follow_ups:
        conn.commit()
    return {"deltas": deltas, "follow_ups": follow_ups}


def _gemini_ok(data_dir: Optional[Path]) -> bool:
    try:
        from gemini_client import gemini_configured

        return bool(gemini_configured(data_dir))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Reshuffle: the Librarian decides, at any time, whether the graph is worth
# updating — and if so, does the work. Callable on a tick, after ingest, after
# an answer, or on demand.
# ---------------------------------------------------------------------------

_META_MINED = "corpus_mined_source_ids"

_RESHUFFLE_SYSTEM = (
    "You curate a user's personal knowledge graph. Given a snapshot of its current "
    "state and recent activity, decide whether it is worth doing maintenance work "
    "RIGHT NOW, and if so what. Available actions:\n"
    "  - 'extract_new': mine entities + questions from newly added, unprocessed sources\n"
    "  - 'merge_duplicates': propose merging entities that look like the same thing\n"
    "  - 'deepen': ask follow-up questions about important but thinly-described nodes\n"
    "Only recommend work the snapshot justifies. If the graph is healthy or nothing "
    "meaningful changed, say should_update=false. Return strict JSON: "
    '{"should_update": bool, "reason": "<short>", "plan": ["extract_new", ...]}'
)

_RESHUFFLE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_update": {"type": "boolean"},
        "reason": {"type": "string"},
        "plan": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["should_update", "reason", "plan"],
}


def _mined_source_ids(conn) -> set[str]:
    from store import meta_get

    try:
        return set(json.loads(meta_get(conn, _META_MINED) or "[]"))
    except Exception:
        return set()


def _mark_mined(conn, source_ids: List[str]) -> None:
    from store import meta_set

    merged = _mined_source_ids(conn) | set(source_ids)
    meta_set(conn, _META_MINED, json.dumps(sorted(merged)[:5000], ensure_ascii=False))


def _new_source_ids(conn) -> List[str]:
    mined = _mined_source_ids(conn)
    rows = conn.execute("SELECT source_id FROM sources ORDER BY updated_at DESC").fetchall()
    return [str(r["source_id"]) for r in rows if str(r["source_id"]) not in mined]


def _graph_snapshot(conn) -> Dict[str, Any]:
    def _count(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    nodes = _count("SELECT COUNT(*) FROM graph_nodes WHERE status NOT IN ('scaffold','stub')")
    edges = _count("SELECT COUNT(*) FROM graph_edges")
    low_conf = _count(
        "SELECT COUNT(*) FROM graph_nodes WHERE status NOT IN ('scaffold','stub') AND confidence < 0.6"
    )
    open_cands = _count("SELECT COUNT(*) FROM graph_candidates WHERE status='open'")
    # Cheap duplicate hint: titles sharing a first token.
    titles = [
        str(r["title"] or "")
        for r in conn.execute(
            "SELECT title FROM graph_nodes WHERE status NOT IN ('scaffold','stub') LIMIT 400"
        ).fetchall()
    ]
    seen: Dict[str, str] = {}
    dups: List[str] = []
    for t in titles:
        first = _norm(t).split(" ")[0] if t.strip() else ""
        if len(first) >= 4 and first in seen and seen[first] != _norm(t):
            dups.append(f"{seen[first]} / {_norm(t)}")
        elif first:
            seen[first] = _norm(t)
    return {
        "nodes": nodes,
        "edges": edges,
        "low_confidence_nodes": low_conf,
        "open_candidates": open_cands,
        "new_unprocessed_sources": len(_new_source_ids(conn)),
        "possible_duplicates": dups[:8],
    }


def reconsider_graph(
    conn,
    data_dir: Optional[Path],
    *,
    force: bool = False,
    trigger: str = "manual",
) -> Dict[str, Any]:
    """Decide whether the graph is worth updating now; if so, do the work.

    The decision is the LLM's (unless force=True). Currently executes the
    'extract_new' action; 'merge_duplicates'/'deepen' are surfaced in the plan for
    the agent/UI and reserved for follow-on work.
    """
    snapshot = _graph_snapshot(conn)
    decision: Dict[str, Any] = {
        "should_update": force,
        "reason": "forced" if force else "",
        "plan": ["extract_new"] if force else [],
    }
    if not force:
        if not _gemini_ok(data_dir):
            return {"status": "no_gemini", "snapshot": snapshot, "decision": decision, "executed": []}
        try:
            from gemini_client import gemini_chat, graph_mine_gemini_model

            raw = gemini_chat(
                system=_RESHUFFLE_SYSTEM,
                messages=[{"role": "user", "content": f"SNAPSHOT:\n{json.dumps(snapshot, indent=2)}"}],
                data_dir=data_dir,
                model=graph_mine_gemini_model(data_dir) if data_dir else None,
                temperature=0.2,
                max_output_tokens=400,
                response_mime_type="application/json",
                response_schema=_RESHUFFLE_SCHEMA,
            )
            d = json.loads(raw)
            decision = {
                "should_update": bool(d.get("should_update")),
                "reason": str(d.get("reason") or "").strip(),
                "plan": [str(p) for p in (d.get("plan") or [])],
            }
        except Exception as exc:
            log.warning("graph reconsider decision failed: %s", exc)
            return {"status": "decision_error", "snapshot": snapshot, "decision": decision, "executed": []}

    if not decision["should_update"]:
        return {"status": "skip", "snapshot": snapshot, "decision": decision, "executed": []}

    executed: List[Dict[str, Any]] = []
    if "extract_new" in decision["plan"] or force:
        new_ids = _new_source_ids(conn)
        if new_ids:
            result = extract_entities_for_sources(conn, data_dir, new_ids)
            _mark_mined(conn, new_ids)
            executed.append({"action": "extract_new", "sources": len(new_ids), "result": result})
    conn.commit()
    return {"status": "ok", "snapshot": snapshot, "decision": decision, "executed": executed}


_bg_lock = threading.Lock()
_bg_running = False


def schedule_background_corpus_extract(
    data_dir: Path,
    *,
    source_ids: Optional[List[str]] = None,
    conn_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Fire-and-forget extraction on a daemon thread; never blocks ingest."""
    global _bg_running
    data = Path(data_dir).expanduser().resolve()
    with _bg_lock:
        if _bg_running:
            return {"status": "skipped", "reason": "already_running"}
        _bg_running = True

    def _worker() -> None:
        global _bg_running
        try:
            if conn_factory is not None:
                conn = conn_factory()
            else:
                from store import DB_FILENAME, connect

                conn = connect(data / DB_FILENAME)
            try:
                extract_entities_for_sources(conn, data, source_ids)
            finally:
                if conn_factory is None:
                    conn.close()
        except Exception:
            log.exception("background corpus extract failed")
        finally:
            with _bg_lock:
                _bg_running = False

    threading.Thread(target=_worker, name="minion-corpus-extract", daemon=True).start()
    return {"status": "scheduled"}
