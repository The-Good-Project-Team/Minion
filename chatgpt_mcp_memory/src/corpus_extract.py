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
    "conversations, and notes. Extract the distinct real-world entities that appear "
    "in the text: people (other than the user themselves), organizations or "
    "companies, and projects or products. For each entity return its label, its "
    "kind (one of: person, organization, project, place, group), and a short "
    "evidence phrase quoting or paraphrasing what the text says about it. "
    "Rules: do NOT invent entities not supported by the text; do NOT include the "
    "user themselves; merge obvious duplicates; prefer proper names over generic "
    "descriptions; skip pure tooling/library noise. Return strict JSON."
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
                },
                "required": ["label", "kind"],
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


def _question_for(kind: str, label: str) -> str:
    """Generic, corpus-agnostic confirmation prompt the user answers in the feed."""
    if kind == "organization":
        return (
            f"**Librarian:** I found **{label}** in what you just added. "
            f"Is this your company or an organization you're part of — and how are you connected?"
        )
    if kind == "project":
        return (
            f"**Librarian:** **{label}** shows up in what you just added. "
            f"Is this a project of yours? A one-liner on what it is helps me file it."
        )
    if kind == "place":
        return f"**Librarian:** Is **{label}** a place that matters to you? How so?"
    if kind == "group":
        return f"**Librarian:** Is **{label}** a group or team you belong to?"
    return (
        f"**Librarian:** I noticed **{label}**. "
        f"Is this someone you know? One line on how you're connected and I'll add them."
    )


def _extract(evidence: str, data_dir: Optional[Path], *, max_entities: int) -> List[Dict[str, Any]]:
    from gemini_client import gemini_chat, graph_mine_gemini_model

    user = (
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

    try:
        raw_entities = _extract(evidence, data_dir, max_entities=max_entities)
    except Exception as exc:
        log.warning("corpus entity extraction failed: %s", exc)
        return {"status": "llm_error", "created": 0, "entities": []}

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
        seen.add(key)
        note = str(ent.get("evidence") or "").strip()[:400]
        graph_candidate_create(
            conn,
            candidate_type=_CANDIDATE_TYPE,
            title=label,
            body_md=_question_for(kind, label),
            payload={
                "label": label,
                "node_kind": kind,
                "evidence": note,
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
    return {"status": "ok", "created": len(created), "entities": created}


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
