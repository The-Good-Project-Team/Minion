"""Identity companion overview and starter thread helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional


def companion_overview(conn, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Small deterministic model of how ready Minion is to organize the user's world."""
    from store import count_sources, graph_scaffold_list, identity_claim_list

    graph = graph_scaffold_list(conn)
    totals = graph.get("totals") or {}
    active_claims = identity_claim_list(conn, status="active", limit=40)
    proposed_claims = identity_claim_list(conn, status="proposed", limit=20)
    source_count = count_sources(conn)

    pillars = [
        _pillar("People", int(totals.get("person") or 0), "Who matters, and how you know them."),
        _pillar("Projects", int(totals.get("project") or 0), "What you are building or carrying."),
        _pillar("Obligations", int(totals.get("obligation") or 0), "Open loops Minion should keep visible."),
        _pillar(
            "Preferences",
            len([c for c in active_claims if str(c.get("kind") or "") == "preference"]),
            "How you like work, writing, and decisions to feel.",
        ),
    ]
    readiness = min(
        100,
        int(source_count > 0) * 20
        + min(40, int(graph.get("user_node_count") or 0) * 8)
        + min(40, len(active_claims) * 6),
    )
    next_steps = _next_steps(source_count=source_count, pillars=pillars, proposed_claims=proposed_claims)
    return {
        "tagline": "Minion is your private identity companion: it helps map people, projects, obligations, and preferences, then uses that map while chatting with you.",
        "readiness": readiness,
        "source_count": source_count,
        "graph_count": int(graph.get("user_node_count") or 0),
        "active_claim_count": len(active_claims),
        "proposed_claim_count": len(proposed_claims),
        "pillars": pillars,
        "next_steps": next_steps,
        "starter_prompts": [
            "Help me organize the people in my world.",
            "Ask me what projects matter right now.",
            "Help me turn my open loops into a clear list.",
            "Learn how I like decisions and writing to sound.",
        ],
        "data_dir": str(Path(data_dir).expanduser()) if data_dir else "",
    }


def _pillar(label: str, count: int, prompt: str) -> Dict[str, Any]:
    return {"label": label, "count": count, "prompt": prompt, "status": "started" if count else "empty"}


def _next_steps(
    *,
    source_count: int,
    pillars: List[Dict[str, Any]],
    proposed_claims: List[Dict[str, Any]],
) -> List[str]:
    steps: List[str] = []
    if source_count <= 0:
        steps.append("Add one source or paste a few notes so Minion has raw context.")
    empty = [p["label"].lower() for p in pillars if int(p.get("count") or 0) == 0]
    if empty:
        steps.append(f"Start mapping {empty[0]} with one short answer.")
    if proposed_claims:
        steps.append("Review proposed identity claims so Minion knows what is true.")
    if len(steps) < 3:
        steps.append("Chat with Minion about what changed today.")
    return steps[:3]


def open_companion_thread(conn) -> Dict[str, Any]:
    """Create or return an open companion thread for free-form organizing chat."""
    from chat_store import chat_message_insert, chat_thread_get, chat_thread_insert, chat_threads_list

    for row in chat_threads_list(conn, status="open", limit=20):
        meta = row.get("meta") or {}
        if meta.get("mode") == "identity_companion":
            full = chat_thread_get(conn, row["thread_id"])
            if full:
                return {"thread": full, "created": False}

    tid = chat_thread_insert(
        conn,
        topic="Minion · identity companion",
        meta={"mode": "identity_companion"},
    )
    chat_message_insert(
        conn,
        thread_id=tid,
        role="assistant",
        body_md=(
            "Let's organize your world. Tell me what you want clearer first: people, projects, obligations, "
            "preferences, or the sources I should connect."
        ),
        meta={"speaker": "Minion", "mode": "identity_companion"},
    )
    return {"thread": chat_thread_get(conn, tid), "created": True}
