"""Corpus-first graph inference: retrieve embedded memory, LLM proposes writes, Python validates."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

ME_NODE = "scaffold-me"
AUTO_WRITE_CONFIDENCE = 0.75
MIN_HIT_SCORE = 0.32
MAX_EVIDENCE_HITS = 16

ALLOWED_NODE_KINDS = frozenset(
    {"person", "place", "organization", "group", "project", "role", "hobby", "asset"}
)
ALLOWED_REL_KINDS = frozenset(
    {
        "knows",
        "related_to",
        "belongs_to",
        "works_at",
        "lives_at",
        "participates_in",
        "owns",
        "manages",
        "founded",
        "parent_of",
        "child_of",
        "married_to",
    }
)

# Common relation verbs the model emits that map cleanly onto an allowed kind.
# Normalizing these (instead of rejecting the whole proposal) keeps a valid node
# creation from being thrown away just because the edge verb was phrased loosely.
_REL_SYNONYMS = {
    "founder_of": "founded",
    "founder": "founded",
    "co_founded": "founded",
    "cofounded": "founded",
    "created": "founded",
    "started": "founded",
    "launched": "founded",
    "employed_by": "works_at",
    "employee_of": "works_at",
    "works_for": "works_at",
    "work_at": "works_at",
    "located_in": "lives_at",
    "located_at": "lives_at",
    "lives_in": "lives_at",
    "based_in": "lives_at",
    "member_of": "participates_in",
    "participant_in": "participates_in",
    "part_of": "belongs_to",
    "owns_company": "owns",
    "runs": "manages",
    "leads": "manages",
    "director_of": "manages",
    "married": "married_to",
    "spouse_of": "married_to",
    "parent": "parent_of",
    "child": "child_of",
    "related": "related_to",
}


def _normalize_rel_kind(rel: str) -> str:
    """Map a loosely-phrased relation onto an allowed kind, else return it unchanged."""
    key = re.sub(r"\s+", "_", str(rel or "").strip().lower())
    if key in ALLOWED_REL_KINDS:
        return key
    return _REL_SYNONYMS.get(key, key)


def sanitize_proposal(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize relation verbs and drop only the edges we still can't map, so one
    loosely-phrased relation never discards the proposal's valid node creations."""
    actions = proposal.get("actions")
    if not isinstance(actions, list):
        return proposal
    cleaned: List[Dict[str, Any]] = []
    for act in actions:
        if str(act.get("type") or "") == "add_edge":
            rel = _normalize_rel_kind(act.get("rel_kind") or "")
            if rel not in ALLOWED_REL_KINDS:
                continue  # drop just this edge, keep the rest of the proposal
            act = {**act, "rel_kind": rel}
        cleaned.append(act)
    proposal["actions"] = cleaned
    return proposal


def build_queries_for_gap(gap: Dict[str, Any]) -> List[str]:
    """3–6 targeted retrieval queries for a graph gap."""
    custom = gap.get("mining_queries")
    if isinstance(custom, list) and custom:
        out: List[str] = []
        seen: Set[str] = set()
        for q in custom:
            q = " ".join(str(q).split()).strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                out.append(q[:200])
            if len(out) >= 6:
                break
        if out:
            return out

    gtype = gap.get("gap_type") or ""
    label = str(gap.get("label") or gap.get("bucket_label") or "").strip()
    hint = str(gap.get("hint") or "").strip()
    queries: List[str] = []

    if gtype == "claim":
        kind = str(gap.get("kind") or "identity")
        queries = [label, f"{kind} {label}", f"who is {label}", f"about {label}"]
    elif gtype == "person":
        queries = [
            label,
            f"who is {label}",
            f"{label} relationship",
            f"how do I know {label}",
            f"about {label}",
        ]
    elif gtype == "person_relation":
        queries = [
            f"how do I know {label}",
            f"{label} coworker friend family",
            f"{label} relationship",
            label,
        ]
    elif gtype == "bucket":
        bucket = str(gap.get("bucket_label") or "")
        queries = [
            hint,
            f"{bucket} {hint}",
            bucket,
            f"my {hint}",
            f"people in {bucket}",
        ]
    elif gtype == "capture":
        app = str(gap.get("app_name") or "")
        title = str(gap.get("window_title") or "")
        queries = [title, f"{app} {title}", app, f"project {title}", f"person {title}"]
    elif gtype == "me_profile":
        queries = [
            "about me biography background",
            "where I was born grew up hometown",
            "my role profession what I do",
            "personal context about myself",
        ]
    elif gtype == "enrich":
        kind = str(gap.get("node_kind") or "")
        queries = [
            label,
            f"who is {label}",
            f"{label} {kind} relationship context updates",
            f"about {label}",
        ]
    else:
        queries = [label or "life graph people projects"]

    out: List[str] = []
    seen: Set[str] = set()
    for q in queries:
        q = " ".join(q.split()).strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append(q[:200])
        if len(out) >= 6:
            break
    return out or ["life context people relationships"]


def _evidence_quality(text: str) -> float:
    """Heuristic multiplier: prefer substantive snippets over AX chrome noise."""
    t = (text or "").strip()
    if len(t) < 40:
        return 0.35
    if len(t) < 100:
        return 0.7
    lower = t.lower()
    chrome_noise = (
        "bookmark this tab",
        "address and search bar",
        "extensions",
        " - pinned",
        "view site information",
    )
    if sum(1 for phrase in chrome_noise if phrase in lower) >= 3:
        return 0.55
    return 1.0


def retrieve_evidence_pack(
    conn,
    gap: Dict[str, Any],
    *,
    top_k_per_query: int = 5,
) -> Dict[str, Any]:
    """Semantic search over embedded chunks; dedupe by chunk_id."""
    from corpus_context import prefetch_for_subject
    from ingest import DEFAULT_MODEL, _embed, _get_model
    from store import search as store_search
    import os

    subject_id = str(gap.get("subject_id") or "")
    label = str(gap.get("label") or gap.get("bucket_label") or "")
    all_hits: Dict[str, Dict[str, Any]] = {}

    for query in build_queries_for_gap(gap):
        pack = prefetch_for_subject(
            conn,
            subject_label=query,
            subject_id=subject_id if query == label or not all_hits else "",
            top_k=top_k_per_query,
            extra_query=label if query != label else "",
        )
        for h in pack.get("hits") or []:
            cid = str(h.get("chunk_id") or "")
            if not cid:
                continue
            prev = all_hits.get(cid)
            if prev is None or float(h.get("score") or 0) > float(prev.get("score") or 0):
                all_hits[cid] = dict(h)

    # Extra wide pass: combine label queries into one embed search
    if label:
        try:
            model = _get_model(os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL))
            combined = " ".join(build_queries_for_gap(gap)[:3])
            vecs = _embed(model, [combined], on_progress=lambda *_: None)
            if vecs.size:
                for h in store_search(conn, vecs[0], top_k=top_k_per_query + 4):
                    cid = h.chunk_id
                    if cid not in all_hits or float(h.score) > float(all_hits[cid].get("score") or 0):
                        all_hits[cid] = {
                            "chunk_id": cid,
                            "score": round(float(h.score), 4),
                            "path": h.path,
                            "kind": h.kind,
                            "text": (h.text or "")[:500],
                        }
        except Exception:
            log.debug("combined embed search skipped", exc_info=True)

    hits = sorted(all_hits.values(), key=lambda x: float(x.get("score") or 0), reverse=True)
    hits = [h for h in hits if float(h.get("score") or 0) >= MIN_HIT_SCORE][:MAX_EVIDENCE_HITS]
    hits.sort(
        key=lambda h: float(h.get("score") or 0) * _evidence_quality(str(h.get("text") or "")),
        reverse=True,
    )
    evidence_refs = [f"chunk:{h['chunk_id']}" for h in hits[:8]]
    if subject_id:
        evidence_refs.append(f"graph:{subject_id}")

    return {
        "hits": hits,
        "evidence_refs": evidence_refs,
        "query_count": len(build_queries_for_gap(gap)),
    }


def _evidence_summary(pack: Dict[str, Any], *, max_hits: int = 3) -> str:
    parts: List[str] = []
    for h in (pack.get("hits") or [])[:max_hits]:
        path = str(h.get("path") or "note")
        snippet = (h.get("text") or "")[:100].replace("\n", " ")
        parts.append(f"{path}: {snippet}")
    return "; ".join(parts)


def validate_proposal(
    proposal: Dict[str, Any],
    gap: Dict[str, Any],
    evidence_pack: Dict[str, Any],
) -> Tuple[bool, str]:
    """Return (ok, reason)."""
    conf = float(proposal.get("confidence") or 0)
    if conf < AUTO_WRITE_CONFIDENCE:
        return False, "confidence_below_threshold"

    actions = proposal.get("actions") or []
    if not actions:
        return False, "no_actions"

    refs = set(evidence_pack.get("evidence_refs") or [])
    chunk_refs = {r for r in refs if r.startswith("chunk:")}
    if not chunk_refs:
        return False, "no_chunk_evidence"

    cited: Set[str] = set()
    for act in actions:
        for ref in act.get("evidence_refs") or []:
            cited.add(str(ref))
    if not cited.intersection(chunk_refs):
        return False, "actions_missing_citations"

    gtype = gap.get("gap_type")
    for act in actions:
        atype = str(act.get("type") or "")
        if atype == "create_node":
            kind = str(act.get("node_kind") or "person")
            title = str(act.get("title") or "").strip()
            if kind not in ALLOWED_NODE_KINDS:
                return False, f"invalid_node_kind:{kind}"
            from graph_fill import _is_plausible_graph_title

            if not _is_plausible_graph_title(title, node_kind=kind):
                return False, "implausible_title"
        if atype == "set_me_profile":
            text = str(act.get("summary") or act.get("user_note") or "").strip()
            if not text:
                return False, "empty_me_profile"
        if atype == "add_edge":
            rel = str(act.get("rel_kind") or "")
            if rel not in ALLOWED_REL_KINDS:
                return False, f"invalid_rel:{rel}"
    if gtype == "claim":
        types = {str(a.get("type")) for a in actions}
        if not types.intersection({"approve_claim", "reject_claim"}):
            return False, "claim_requires_decision"

    uq = str(proposal.get("unresolved_question") or "").strip()
    if uq and conf < 0.9:
        return False, "ambiguous"

    return True, "ok"


def apply_proposal(
    conn,
    gap: Dict[str, Any],
    proposal: Dict[str, Any],
    *,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Apply validated LLM actions using graph_fill helpers."""
    from entity_resolution import ensure_person_node, link_belongs_to_scaffold
    from graph_fill import (
        ME_NODE,
        _create_graph_node,
        _default_parent_for_kind,
        _link_knows,
        _log_deltas,
        _person_summary,
        _people_root,
        persist_graph_snapshot,
    )
    from store import identity_claim_set_status

    deltas: List[str] = []
    gtype = gap.get("gap_type")
    subject_id = gap.get("subject_id")
    evidence_refs = list(proposal.get("evidence_refs") or [])
    stability = str(proposal.get("stability") or gap.get("stability") or "active")

    actions = list(proposal.get("actions") or [])

    # Map proposed identifiers (the loose ids/titles the LLM emits) → the real
    # graph node ids we create or already have. add_edge endpoints are resolved
    # against this map so we never write an edge to a node that does not exist.
    created: Dict[str, str] = {}

    def _register(real_id: str, *aliases: str) -> None:
        if not real_id:
            return
        for alias in aliases:
            key = _norm_key(alias)
            if key:
                created.setdefault(key, real_id)

    me_aliases = {"me", "myself", "i", "self", "you", "user", "owner", _norm_key(ME_NODE)}

    def _resolve_endpoint(raw: str) -> Optional[str]:
        """Map a proposed endpoint to a real node id, or None if it cannot be
        resolved (in which case the edge is skipped instead of dangling)."""
        key = _norm_key(raw)
        if not key or key in me_aliases:
            return ME_NODE
        if key in created:
            return created[key]
        if raw and _node_exists(conn, raw):
            return raw
        return None

    # --- Pass 1: everything except edges, so created nodes exist before linking. ---
    edge_actions: List[Dict[str, Any]] = []
    for act in actions:
        atype = str(act.get("type") or "")
        act_refs = list(act.get("evidence_refs") or evidence_refs)

        if atype == "add_edge":
            edge_actions.append(act)
        elif atype == "set_me_profile":
            text = str(act.get("summary") or act.get("user_note") or "").strip()
            if text:
                _me_profile(conn, text, act_refs, stability=stability)
                deltas.append("Filled **your profile** from saved context.")
        elif atype == "approve_claim":
            cid = str(act.get("claim_id") or gap.get("claim_id") or "")
            if cid:
                identity_claim_set_status(conn, cid, "active")
                deltas.append("Approved mirror claim from your saved context.")
        elif atype == "reject_claim":
            cid = str(act.get("claim_id") or gap.get("claim_id") or "")
            if cid:
                identity_claim_set_status(conn, cid, "rejected")
                deltas.append("Rejected mirror claim based on context.")
        elif atype == "set_person_summary":
            nid = str(act.get("node_id") or subject_id or "")
            text = str(act.get("summary") or act.get("user_note") or "").strip()
            if nid and text:
                _person_summary(conn, nid, text)
                _set_node_evidence(conn, nid, act_refs, float(act.get("confidence") or proposal.get("confidence") or 0.8))
                _mark_stability(conn, nid, stability)
                _register(nid, nid, str(act.get("node_id") or ""), _node_title(conn, nid))
                deltas.append(f"Filled **{_node_title(conn, nid)}** from your notes.")
        elif atype == "create_node":
            kind = str(act.get("node_kind") or "person")
            title = str(act.get("title") or "").strip()
            parent = str(act.get("parent_node_id") or gap.get("parent_node_id") or _default_parent_for_kind(kind))
            note = str(act.get("user_note") or act.get("summary") or "")
            if kind == "person" and parent == "scaffold-people-friends":
                nid = ensure_person_node(
                    conn,
                    label=title,
                    meta={
                        "user_note": note[:500],
                        "source": "corpus_infer",
                        "evidence_refs": act_refs,
                        "stability": stability,
                    },
                )
            else:
                nid = _create_graph_node(conn, parent, kind, title, user_note=note)
                if kind == "person":
                    link_belongs_to_scaffold(conn, nid, _people_root(parent))
                    _link_knows(conn, ME_NODE, nid, note)
            _set_node_evidence(conn, nid, act_refs, float(act.get("confidence") or 0.8))
            _mark_stability(conn, nid, stability)
            _register(nid, nid, str(act.get("node_id") or ""), title)
            deltas.append(f"Added **{title}** to your graph from saved context.")

    # --- Pass 2: edges, resolved to real node ids only (no dangling endpoints). ---
    for act in edge_actions:
        act_refs = list(act.get("evidence_refs") or evidence_refs)
        fr = _resolve_endpoint(str(act.get("from_node_id") or ""))
        to = _resolve_endpoint(str(act.get("to_node_id") or subject_id or ""))
        rel = str(act.get("rel_kind") or "knows")
        note = str(act.get("relation_note") or act.get("summary") or "")
        if not fr or not to or fr == to:
            # Unresolvable or self-referential endpoint — skip rather than write a
            # dangling edge that points at a non-existent node.
            continue
        if rel == "knows":
            _link_knows(conn, fr, to, note)
            _set_node_evidence(conn, to, act_refs, float(act.get("confidence") or 0.8))
            deltas.append(f"Linked you → **{_node_title(conn, to)}** from context.")
        else:
            _insert_edge(conn, fr, to, rel, act_refs, float(act.get("confidence") or 0.8))
            deltas.append(f"Linked graph ({rel}).")

    gap_exhausted = _gap_resolved(conn, gap, gtype)
    out = {
        "ok": True,
        "deltas": deltas,
        "filled": bool(deltas),
        "gap_exhausted": gap_exhausted,
        "resolved": gap_exhausted,
    }
    if deltas:
        persist_graph_snapshot(conn, data_dir)
        _log_deltas(data_dir, out)
    return out


def try_fill_gap_from_corpus(
    conn,
    gap: Dict[str, Any],
    *,
    data_dir: Optional[Path] = None,
    mining_kind: str = "ongoing",
    stability: str = "active",
) -> Dict[str, Any]:
    """
    Attempt to fill a gap from embedded corpus before asking the user.
    Returns status: filled | needs_question | skipped.
    """
    from gemini_client import gemini_configured
    from librarian_llm import propose_graph_actions_from_evidence

    if not gemini_configured(data_dir):
        return {"status": "skipped", "reason": "no_gemini"}

    pack = retrieve_evidence_pack(conn, gap)
    if not pack.get("hits"):
        return {"status": "skipped", "reason": "no_evidence"}

    proposal, used_llm = propose_graph_actions_from_evidence(
        conn,
        gap,
        pack,
        data_dir=data_dir,
        mining_kind=mining_kind,
    )
    if not used_llm or not proposal:
        return {"status": "skipped", "reason": "llm_unavailable"}

    proposal["stability"] = stability
    sanitize_proposal(proposal)

    ok, reason = validate_proposal(proposal, gap, pack)
    if not ok:
        uq = str(proposal.get("unresolved_question") or "").strip()
        if uq or reason == "ambiguous":
            cite = _evidence_summary(pack)
            q = uq or _default_ambiguous_question(gap, cite)
            return {
                "status": "needs_question",
                "ambiguous_question": q,
                "evidence_cite": cite,
                "proposal": proposal,
            }
        return {"status": "skipped", "reason": reason}

    result = apply_proposal(conn, gap, proposal, data_dir=data_dir)
    if result.get("filled") and result.get("gap_exhausted"):
        return {
            "status": "filled",
            "deltas": result.get("deltas"),
            "gap_exhausted": True,
            "proposal": proposal,
        }
    if result.get("filled"):
        return {
            "status": "filled",
            "deltas": result.get("deltas"),
            "gap_exhausted": False,
            "proposal": proposal,
        }

    uq = str(proposal.get("unresolved_question") or "").strip()
    if uq:
        return {
            "status": "needs_question",
            "ambiguous_question": uq,
            "evidence_cite": _evidence_summary(pack),
            "proposal": proposal,
        }
    return {"status": "skipped", "reason": "apply_failed"}


def _default_ambiguous_question(gap: Dict[str, Any], cite: str) -> str:
    label = gap.get("label") or gap.get("bucket_label") or "this"
    body = f"I found conflicting notes about **{label}**. Which fits?"
    if cite:
        body += f"\n\nFrom your context: {cite}"
    return body


def _gap_resolved(conn, gap: Dict[str, Any], gtype: Optional[str]) -> bool:
    from graph_fill import pick_next_gap

    gtype = gtype or gap.get("gap_type")
    if gtype == "claim":
        cid = gap.get("claim_id")
        if cid:
            row = conn.execute(
                "SELECT status FROM identity_claims WHERE claim_id=?", (cid,)
            ).fetchone()
            return row is not None and str(row["status"]) != "proposed"
    if gtype == "person" and gap.get("subject_id"):
        row = conn.execute(
            "SELECT summary FROM graph_nodes WHERE node_id=?", (gap["subject_id"],)
        ).fetchone()
        if row and row["summary"] and str(row["summary"]).strip() not in ("", "{}"):
            return True
    if gtype in ("enrich", "me_profile") and gap.get("subject_id"):
        row = conn.execute(
            "SELECT summary, confidence FROM graph_nodes WHERE node_id=?",
            (gap["subject_id"],),
        ).fetchone()
        if row and float(row["confidence"] or 0) >= 0.72:
            raw = str(row["summary"] or "")
            if raw and raw not in ("{}", "") and len(raw) > 15:
                return True
    if gtype == "person_relation" and gap.get("subject_id"):
        row = conn.execute(
            "SELECT 1 FROM graph_edges WHERE from_node_id=? AND to_node_id=? "
            "AND rel_kind IN ('knows', 'related_to')",
            (ME_NODE, gap["subject_id"]),
        ).fetchone()
        return row is not None
    if gtype == "bucket":
        parent_id = gap.get("parent_node_id")
        if parent_id:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM graph_nodes "
                "WHERE parent_node_id=? AND status NOT IN ('scaffold', 'stub')",
                (parent_id,),
            ).fetchone()
            return n is not None and int(n["c"]) > 0
    # Re-check: if this exact gap would not be picked again
    nxt = pick_next_gap(conn, None)
    if not nxt:
        return True
    return nxt.get("gap_type") != gtype or nxt.get("subject_id") != gap.get("subject_id")


def _node_title(conn, node_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    return str(row["title"]) if row else "them"


def _norm_key(value: str) -> str:
    """Normalize a proposed id/title for endpoint matching (case/space-insensitive)."""
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _node_exists(conn, node_id: str) -> bool:
    if not node_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    return row is not None


def _set_node_evidence(
    conn, node_id: str, refs: List[str], confidence: float
) -> None:
    row = conn.execute(
        "SELECT source_refs_json, confidence FROM graph_nodes WHERE node_id=?",
        (node_id,),
    ).fetchone()
    if not row:
        return
    try:
        existing = json.loads(row["source_refs_json"] or "[]")
    except json.JSONDecodeError:
        existing = []
    merged = list(dict.fromkeys([*existing, *refs]))[:20]
    conf = max(float(row["confidence"] or 0), confidence)
    conn.execute(
        "UPDATE graph_nodes SET source_refs_json=?, confidence=?, updated_at=? "
        "WHERE node_id=?",
        (json.dumps(merged, ensure_ascii=False), conf, time.time(), node_id),
    )


def _insert_edge(
    conn,
    from_id: str,
    to_id: str,
    rel_kind: str,
    refs: List[str],
    confidence: float,
) -> None:
    from store import _new_id

    exists = conn.execute(
        "SELECT 1 FROM graph_edges WHERE from_node_id=? AND to_node_id=? AND rel_kind=?",
        (from_id, to_id, rel_kind),
    ).fetchone()
    if exists:
        return
    eid = _new_id("ge")
    now = time.time()
    conn.execute(
        "INSERT INTO graph_edges(edge_id, from_node_id, to_node_id, rel_kind, "
        "confidence, source_refs_json, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid,
            from_id,
            to_id,
            rel_kind,
            confidence,
            json.dumps(refs, ensure_ascii=False),
            now,
            now,
        ),
    )


def _me_profile(conn, text: str, refs: List[str], *, stability: str = "core") -> None:
    row = conn.execute(
        "SELECT summary, source_refs_json, confidence FROM graph_nodes WHERE node_id=?",
        (ME_NODE,),
    ).fetchone()
    if not row:
        return
    summary = _parse_summary_row(row["summary"])
    summary["user_profile"] = text[:2000]
    summary["stability"] = stability
    summary["source"] = "corpus_infer"
    refs_merged = list(
        dict.fromkeys([*_parse_json_list(row["source_refs_json"]), *refs])
    )[:20]
    conf = max(float(row["confidence"] or 0), 0.78)
    conn.execute(
        "UPDATE graph_nodes SET summary=?, source_refs_json=?, confidence=?, updated_at=? "
        "WHERE node_id=?",
        (
            json.dumps(summary, ensure_ascii=False),
            json.dumps(refs_merged, ensure_ascii=False),
            conf,
            time.time(),
            ME_NODE,
        ),
    )


def _mark_stability(conn, node_id: str, stability: str) -> None:
    row = conn.execute(
        "SELECT summary FROM graph_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if not row:
        return
    summary = _parse_summary_row(row["summary"])
    summary["stability"] = stability
    conn.execute(
        "UPDATE graph_nodes SET summary=?, updated_at=? WHERE node_id=?",
        (json.dumps(summary, ensure_ascii=False), time.time(), node_id),
    )


def _parse_summary_row(raw: Any) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("{"):
        try:
            val = json.loads(text)
            return val if isinstance(val, dict) else {"user_note": text}
        except json.JSONDecodeError:
            pass
    return {"user_note": text} if text else {}


def _parse_json_list(raw: Any) -> List[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    return [str(x) for x in val] if isinstance(val, list) else []
