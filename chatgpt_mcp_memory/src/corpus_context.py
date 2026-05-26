"""Prefetch indexed corpus context for council proposals and chat."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ingest import DEFAULT_MODEL, _embed, _get_model
from store import search as store_search


def prefetch_for_subject(
    conn,
    *,
    subject_label: str,
    subject_id: str = "",
    top_k: int = 5,
    extra_query: str = "",
) -> Dict[str, Any]:
    """Semantic search over existing chunks for a graph subject."""
    parts = [subject_label.strip(), extra_query.strip()]
    query = " ".join(p for p in parts if p)
    hits: List[Dict[str, Any]] = []
    if not query:
        return {"hits": hits, "wiki_excerpt": "", "evidence_refs": []}

    try:
        model = _get_model(os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL))
        vecs = _embed(model, [query], on_progress=lambda *_: None)
        if vecs.size:
            search_fn = store_search
            if subject_id:
                try:
                    from graph_retrieval import neighborhood_search

                    search_fn = lambda c, v, **kw: neighborhood_search(c, v, query, **kw)
                except Exception:
                    search_fn = store_search
            for h in search_fn(conn, vecs[0], top_k=top_k):
                hits.append(
                    {
                        "chunk_id": h.chunk_id,
                        "score": round(float(h.score), 4),
                        "path": h.path,
                        "kind": h.kind,
                        "text": (h.text or "")[:400],
                    }
                )
    except Exception:
        pass

    wiki_excerpt = ""
    if subject_id:
        row = conn.execute(
            "SELECT summary, body_md, title FROM graph_nodes WHERE node_id=?",
            (subject_id,),
        ).fetchone()
        if row:
            wiki_excerpt = str(row["title"] or subject_label)
            raw = row["summary"] or row["body_md"] or ""
            if raw and str(raw).strip().startswith("{"):
                try:
                    meta = json.loads(raw)
                    wiki_excerpt = json.dumps(meta, ensure_ascii=False)[:300]
                except json.JSONDecodeError:
                    wiki_excerpt = str(raw)[:300]
            elif raw:
                wiki_excerpt = str(raw)[:300]

    evidence_refs = [f"chunk:{h['chunk_id']}" for h in hits[:3]]
    if subject_id:
        evidence_refs.append(f"graph:{subject_id}")

    return {
        "hits": hits,
        "wiki_excerpt": wiki_excerpt,
        "evidence_refs": evidence_refs,
    }


def corpus_summary_line(corpus: Dict[str, Any], *, max_hits: int = 2) -> str:
    """One-line counsel hint from corpus prefetch."""
    hits = corpus.get("hits") or []
    if not hits:
        return ""
    parts = []
    for h in hits[:max_hits]:
        path = str(h.get("path") or "note")
        label = os.path.basename(path) or path
        snippet = (h.get("text") or "")[:80].replace("\n", " ")
        parts.append(f"{label}: {snippet}" if snippet else label)
    return "From your notes — " + "; ".join(parts)
