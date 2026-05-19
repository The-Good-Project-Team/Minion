"""Butler-style context composition: Today bundle + working_context for MCP."""
from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import os

import numpy as np

import identity
import screen_context_store
from consent_policy import filter_hits_for_mcp
from ingest import DEFAULT_MODEL, _embed, _get_model
from retrieval_bias import apply_identity_rerank
from store import (
    ambient_events_since,
    identity_claim_list,
    search as store_search,
    system_issues_open,
    task_list,
)


def _tokens_from_text(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]{3,}", (text or "").lower())
    stop = frozenset(
        "the and for are but not you all can had her was one our out day get has "
        "how its may new now see two way who any per ran sit try why ask own also "
        "just more most much than then them well will with this that from they been "
        "have what when your about into over such only".split()
    )
    return [w for w in words if w not in stop][:12]


def _prefetch_memory_hits(
    conn,
    data_dir: Path,
    *,
    query_text: str,
    top_k: int = 5,
    for_mcp: bool = False,
) -> List[Dict[str, Any]]:
    q = query_text.strip()
    if not q:
        return []
    try:
        model = _get_model(os.environ.get("MINION_EMBED_MODEL", DEFAULT_MODEL))
        vecs = _embed(model, [q], on_progress=lambda *_: None)
        if not vecs.size:
            return []
        hits = store_search(conn, vecs[0], top_k=top_k)
        if for_mcp:
            hits = filter_hits_for_mcp(hits, data_dir)
        hits, _ = apply_identity_rerank(conn, hits)
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for h in hits[:top_k]:
        out.append(
            {
                "chunk_id": h.chunk_id,
                "score": round(float(h.score), 4),
                "path": h.path,
                "kind": h.kind,
                "text": (h.text or "")[:400],
            }
        )
    return out


def build_working_context(
    conn,
    data_dir: Path,
    *,
    for_mcp: bool = True,
    memory_top_k: int = 3,
) -> Dict[str, Any]:
    """Compose focus + attention + prefetched memory for MCP initialize / get_working_context."""
    focus = screen_context_store.current_record(data_dir)
    recent = screen_context_store.read_recent(data_dir, limit=40)
    since_1h = time.time() - 3600.0
    hour_events = [e for e in recent if float(e.get("ts") or 0) >= since_1h]
    apps = Counter(str(e.get("app_name") or "?") for e in hour_events)
    attention_summary = ", ".join(f"{a} ({c})" for a, c in apps.most_common(6))
    try:
        from attention_rollup import attention_excerpt_for_mcp, rollup_attention

        rollup = rollup_attention(conn, since_ts=since_1h, limit=400)
        rollup_line = attention_excerpt_for_mcp(rollup)
        if rollup_line:
            attention_summary = rollup_line
    except Exception:
        pass

    query_parts: List[str] = []
    if focus:
        query_parts.append(str(focus.get("window_title") or ""))
        ax = str(focus.get("ax_text_sample") or "")
        if ax:
            query_parts.append(ax[:200])
    query_text = " ".join(query_parts)
    memory_hits = _prefetch_memory_hits(
        conn, data_dir, query_text=query_text, top_k=memory_top_k, for_mcp=for_mcp
    )

    active_claims = identity_claim_list(conn, status="active", limit=8)
    claim_excerpt = [
        {"kind": c["kind"], "text": c["text"][:200]} for c in active_claims[:5]
    ]

    council_excerpt: List[Dict[str, Any]] = []
    if for_mcp:
        try:
            from council_store import council_proposals_list_open

            for p in council_proposals_list_open(conn, limit=2):
                council_excerpt.append(
                    {
                        "title": p["title"],
                        "summary": (p["summary"] or "")[:160],
                        "intensity": p["intensity"],
                    }
                )
        except Exception:
            pass

    return {
        "status": "ok" if focus else "empty",
        "focus": focus,
        "attention_last_hour": {
            "event_count": len(hour_events),
            "top_apps": attention_summary,
            "recent_titles": [
                {
                    "ts": e.get("ts"),
                    "app_name": e.get("app_name"),
                    "window_title": e.get("window_title"),
                }
                for e in hour_events[:8]
            ],
        },
        "related_memory": memory_hits,
        "identity_excerpt": claim_excerpt,
        "open_council_proposals": council_excerpt,
        "composed_at": time.time(),
    }


def build_today_bundle(conn, data_dir: Path) -> Dict[str, Any]:
    """GET /today — butler screen aggregate."""
    working = build_working_context(conn, data_dir, for_mcp=False, memory_top_k=5)
    since_24h = time.time() - 86400.0
    ambient = ambient_events_since(conn, since_ts=since_24h, limit=200)
    rollup_24h = {}
    try:
        from attention_rollup import rollup_attention

        rollup_24h = rollup_attention(conn, since_ts=since_24h, limit=500)
    except Exception:
        pass
    apps_24h = Counter(
        str((e.get("payload") or {}).get("app_name") or "?") for e in ambient
    )
    attention_24h = [
        {
            "ts": e["captured_at"],
            "app_name": (e.get("payload") or {}).get("app_name"),
            "window_title": (e.get("payload") or {}).get("window_title"),
            "event_id": e["event_id"],
        }
        for e in ambient[:12]
    ]

    work_open = task_list(
        conn,
        status="open",
        limit=20,
    )
    work_review = task_list(conn, status="review", limit=20)
    work_proposed = [
        t for t in task_list(conn, limit=30) if t.get("origin") in ("inferred", "agent")
        and t.get("status") in ("open", "review")
    ]

    issues = system_issues_open(conn, limit=10)
    identity_md = identity.build_identity_summary(conn, max_claims=12, max_clusters=4)

    return {
        "working_context": working,
        "attention_24h": {
            "top_apps": rollup_24h.get("summary_line")
            or ", ".join(f"{a} ({c})" for a, c in apps_24h.most_common(8)),
            "recent": attention_24h,
            "rollup": rollup_24h,
        },
        "work_items": {
            "open": work_open,
            "review": work_review,
            "inferred_pending": work_proposed,
        },
        "needs_attention": issues,
        "identity_excerpt_md": identity_md[:2000],
    }
