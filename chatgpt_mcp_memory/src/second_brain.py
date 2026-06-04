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
from store import (
    ambient_events_since,
    identity_claim_list,
    search as store_search,
    system_issues_open,
    task_list,
)

_DEADLINE_RE = re.compile(
    r"\b("
    r"due|deadline|tomorrow|tonight|today|morning|review call|ship|urgent|"
    r"may\s+\d{1,2}|monday|tuesday|wednesday|thursday|friday"
    r")\b",
    re.IGNORECASE,
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
        # Identity/graph reranking removed from retrieval — kept as its own
        # surface so it can't degrade relevance (see retrieval_bias).
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


def _clip(text: Any, limit: int = 220) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit]


def _task_priority_score(task: Dict[str, Any], now_ts: float) -> float:
    score = 0.2
    pri = str(task.get("priority") or "").lower()
    if pri in ("urgent", "high"):
        score += 0.4
    elif pri in ("medium", "normal"):
        score += 0.2
    due = task.get("due_at")
    if due is not None:
        hours = (float(due) - now_ts) / 3600.0
        if hours <= 0:
            score += 0.45
        elif hours <= 24:
            score += 0.4
        elif hours <= 72:
            score += 0.25
    if task.get("status") == "review":
        score += 0.15
    return min(score, 0.95)


def _recent_deadline_signals(
    ambient: List[Dict[str, Any]],
    *,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for e in ambient:
        payload = e.get("payload") or {}
        title = _clip(payload.get("window_title"), 120)
        text = _clip(payload.get("ax_text_sample"), 320)
        haystack = " ".join([title, text])
        if not haystack or not _DEADLINE_RE.search(haystack):
            continue
        key = re.sub(r"\W+", " ", haystack).casefold()[:180]
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "ts": e.get("captured_at"),
                "app_name": payload.get("app_name"),
                "window_title": title,
                "snippet": text or title,
                "event_id": e.get("event_id"),
            }
        )
        if len(out) >= limit:
            break
    return out


_CORPUS_STEP_QUERIES = (
    "deadline tomorrow morning review call urgent due ship",
    "client deliverable website LinkedIn marketing launch",
    "pitch video fundraising investor deck",
    "Next.js migration website deploy ship padding",
)


def _corpus_next_step_signals(
    conn,
    data_dir: Path,
    ambient: List[Dict[str, Any]],
    *,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Surface deadline/project hints from embedded memory, not just live screen text."""
    queries: List[str] = list(_CORPUS_STEP_QUERIES)
    apps = Counter(
        str((e.get("payload") or {}).get("app_name") or "")
        for e in ambient
        if (e.get("payload") or {}).get("app_name")
    )
    noisy = frozenset(
        {
            "",
            "?",
            "process_snapshot",
            "mouse_event",
            "keyboard_event",
            "app_launched",
        }
    )
    top_apps = [a for a, _ in apps.most_common(4) if a not in noisy]
    if top_apps:
        queries.append(f"{' '.join(top_apps[:3])} active work project")

    pooled: Dict[str, Dict[str, Any]] = {}
    for query in queries[:6]:
        for hit in _prefetch_memory_hits(conn, data_dir, query_text=query, top_k=3):
            chunk_id = str(hit.get("chunk_id") or "")
            if not chunk_id:
                continue
            score = float(hit.get("score") or 0.0)
            text = str(hit.get("text") or "")
            if score < 0.42:
                continue
            if not _DEADLINE_RE.search(text) and score < 0.52:
                continue
            prev = pooled.get(chunk_id)
            if prev is None or score > float(prev.get("score") or 0):
                pooled[chunk_id] = {**hit, "query": query, "score": score}

    ranked = sorted(pooled.values(), key=lambda h: float(h.get("score") or 0), reverse=True)
    out: List[Dict[str, Any]] = []
    for hit in ranked[:limit]:
        score = float(hit.get("score") or 0.0)
        text = str(hit.get("text") or "")
        out.append(
            {
                "kind": "corpus_signal",
                "title": _clip(text, 160),
                "why": f"Saved context matches “{str(hit.get('query') or '')[:48]}” (score {score:.2f}).",
                "confidence": round(min(0.92, score + 0.08), 2),
                "evidence": [
                    {
                        "chunk_id": hit.get("chunk_id"),
                        "path": hit.get("path"),
                        "kind": hit.get("kind"),
                        "score": score,
                        "snippet": _clip(text, 220),
                    }
                ],
            }
        )
    return out


def build_next_steps(
    conn,
    data_dir: Path,
    *,
    now_ts: Optional[float] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    """Rank near-term work from local screen context, tasks, corpus, and system health."""
    now = float(now_ts or time.time())
    since_36h = now - 36 * 3600.0
    try:
        ambient = ambient_events_since(conn, since_ts=since_36h, limit=500)
    except Exception:
        ambient = []

    items: List[Dict[str, Any]] = []
    for issue in system_issues_open(conn, limit=3):
        items.append(
            {
                "kind": "system_issue",
                "title": _clip(issue.get("body_md"), 140) or issue.get("issue_id"),
                "why": f"{issue.get('severity', 'issue')} from {issue.get('source_key') or 'system'}",
                "confidence": 0.9 if issue.get("severity") == "error" else 0.75,
                "evidence": [{"issue_id": issue.get("issue_id")}],
            }
        )

    tasks = task_list(conn, status="review", limit=10) + task_list(
        conn, status="open", limit=25
    )
    for task in tasks:
        score = _task_priority_score(task, now)
        evidence = []
        if task.get("due_at") is not None:
            evidence.append({"due_at": task.get("due_at")})
        if task.get("context_refs"):
            evidence.append({"context_refs": task.get("context_refs")[:2]})
        items.append(
            {
                "kind": "task",
                "title": _clip(task.get("title"), 160),
                "why": _clip(task.get("body_md"), 220)
                or f"{task.get('status', 'open')} {task.get('origin', 'task')} task",
                "confidence": round(score, 2),
                "evidence": evidence,
                "task_id": task.get("task_id"),
                "status": task.get("status"),
            }
        )

    deadline_signals = _recent_deadline_signals(ambient, limit=20)
    for sig in deadline_signals[:3]:
        title = sig.get("window_title") or sig.get("snippet") or "Recent deadline signal"
        items.append(
            {
                "kind": "screen_signal",
                "title": _clip(title, 160),
                "why": "Recent screen text contains deadline or urgency language.",
                "confidence": 0.68,
                "evidence": [sig],
            }
        )

    corpus_signals = _corpus_next_step_signals(conn, data_dir, ambient, limit=3)
    items.extend(corpus_signals)

    items.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)
    return {
        "items": items[: max(1, min(limit, 12))],
        "signals": {
            "ambient_events_scanned": len(ambient),
            "deadline_signals": len(deadline_signals),
            "task_candidates": len(tasks),
            "corpus_signals": len(corpus_signals),
        },
        "generated_at": now,
    }


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
    context_pack: Dict[str, Any] = {}
    try:
        from context_core import context_bundle

        subject = ""
        if focus:
            subject = str(focus.get("window_title") or focus.get("app_name") or "")[:120]
        context_pack = context_bundle(
            conn, data_dir, subject=subject, for_mcp=for_mcp, memory_top_k=memory_top_k
        )
    except Exception:
        context_pack = {}

    graph_excerpt = {
        "user_node_count": (context_pack.get("graph") or {}).get("user_node_count", 0),
        "totals": (context_pack.get("graph") or {}).get("totals") or {},
        "open_candidates": (context_pack.get("open_candidates") or [])[:3],
    }
    graph_spine_md = (context_pack.get("spine_md") or "")[:1200]

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
        "graph_context": graph_excerpt,
        "graph_spine_md": graph_spine_md,
        "context_bundle": context_pack,
        "context_md": (context_pack.get("context_md") or "")[:2000],
        "why_this_context": context_pack.get("why_this_context") or [],
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
        "next_steps": build_next_steps(conn, data_dir, now_ts=time.time(), limit=5),
        "needs_attention": issues,
        "identity_excerpt_md": identity_md[:2000],
    }
