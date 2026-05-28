"""Resource polls and connector intents — assume data sources, persist build work."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

POLL_FILENAME = "onboarding/resource_poll.json"

# Canonical resources Minion can ask about and scaffold connectors for.
RESOURCE_CATALOG: List[Dict[str, Any]] = [
    {
        "resource_id": "gmail",
        "label": "Gmail",
        "question": "Do you use Gmail for email?",
        "connector_kind": "export_or_api",
        "import_hint": "Google Takeout mbox or OAuth when available",
    },
    {
        "resource_id": "chatgpt",
        "label": "ChatGPT",
        "question": "Do you use ChatGPT?",
        "connector_kind": "export",
        "import_hint": "Drop ChatGPT data export JSON into inbox",
    },
    {
        "resource_id": "claude",
        "label": "Claude",
        "question": "Do you use Claude (Anthropic)?",
        "connector_kind": "export",
        "import_hint": "Drop Claude export into inbox",
    },
    {
        "resource_id": "slack",
        "label": "Slack",
        "question": "Do you use Slack for work chat?",
        "connector_kind": "export",
        "import_hint": "Workspace export zip into inbox",
    },
    {
        "resource_id": "local_folders",
        "label": "Local folders",
        "question": "Do you keep important files in local folders (Documents, Desktop)?",
        "connector_kind": "watch",
        "import_hint": "Point Minion inbox or enable folder watch",
    },
    {
        "resource_id": "calendar",
        "label": "Calendar",
        "question": "Do you use Apple Calendar for scheduling?",
        "connector_kind": "macos",
        "import_hint": "Calendar permission + life_evidence sync",
    },
]


def _poll_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / POLL_FILENAME


def load_resource_poll(data_dir: Path) -> Dict[str, Any]:
    p = _poll_path(data_dir)
    if not p.is_file():
        return {"answers": {}, "updated_at": None, "resources": RESOURCE_CATALOG}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"answers": {}, "updated_at": None, "resources": RESOURCE_CATALOG}
    if not isinstance(raw.get("answers"), dict):
        raw["answers"] = {}
    raw["resources"] = RESOURCE_CATALOG
    return raw


def save_resource_poll(data_dir: Path, answers: Dict[str, Any]) -> Dict[str, Any]:
    p = _poll_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"answers": answers, "updated_at": time.time()}
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def next_poll_question(data_dir: Path) -> Optional[Dict[str, Any]]:
    """First unanswered resource in catalog order."""
    state = load_resource_poll(data_dir)
    answers = state.get("answers") or {}
    for res in RESOURCE_CATALOG:
        rid = res["resource_id"]
        if rid not in answers:
            return {**res, "poll_index": RESOURCE_CATALOG.index(res), "total": len(RESOURCE_CATALOG)}
    return None


def record_poll_answer(
    conn,
    data_dir: Path,
    *,
    resource_id: str,
    answer: bool,
    free_text: str = "",
) -> Dict[str, Any]:
    """Persist yes/no (+ optional note); spawn connector work when user opts in."""
    state = load_resource_poll(data_dir)
    answers = dict(state.get("answers") or {})
    answers[resource_id] = {
        "uses": bool(answer),
        "note": (free_text or "").strip()[:500],
        "answered_at": time.time(),
    }
    save_resource_poll(data_dir, answers)

    created: Dict[str, Any] = {"candidate_id": None, "task_id": None}
    if answer:
        created = create_connector_intent(
            conn,
            resource_id=resource_id,
            user_note=free_text,
            source="resource_poll",
        )

    nxt = next_poll_question(data_dir)
    return {
        "ok": True,
        "resource_id": resource_id,
        "uses": answer,
        "next_question": nxt,
        "poll_complete": nxt is None,
        **created,
    }


def create_connector_intent(
    conn,
    *,
    resource_id: str,
    user_note: str = "",
    source: str = "onboarding",
    custom_label: str = "",
) -> Dict[str, Any]:
    """Graph candidate + task for building a connector."""
    from store import graph_candidate_create, graph_candidate_list, task_infer_insert, task_list

    catalog = {r["resource_id"]: r for r in RESOURCE_CATALOG}
    spec = catalog.get(resource_id)
    label = custom_label or (spec.get("label") if spec else resource_id)
    title = f"Connect {label}"
    body = (user_note or "").strip()
    if spec and not body:
        body = str(spec.get("import_hint") or "")

    # Dedupe open connector intents for same resource
    for c in graph_candidate_list(conn, status="open", limit=50):
        payload = c.get("payload") or {}
        if c.get("candidate_type") == "connector_intent" and payload.get("resource_id") == resource_id:
            return {"candidate_id": c.get("candidate_id"), "task_id": payload.get("task_id"), "deduped": True}

    payload = {
        "resource_id": resource_id,
        "connector_kind": (spec or {}).get("connector_kind", "custom"),
        "import_hint": (spec or {}).get("import_hint", ""),
        "user_note": body[:500],
        "source": source,
    }
    cid = graph_candidate_create(
        conn,
        candidate_type="connector_intent",
        title=title[:300],
        body_md=body[:4000] or f"Minion will scaffold a local connector for {label}.",
        payload=payload,
        confidence=0.7,
        source=source[:64],
    )

    tid = task_infer_insert(
        conn,
        title=title[:500],
        body_md=body[:4000] or f"Build connector for {label}. {payload.get('import_hint', '')}",
        origin="connector_intent",
        context_refs=[{"type": "graph_candidate", "id": cid}],
    )
    payload["task_id"] = tid
    from store import graph_candidate_resolve

    graph_candidate_resolve(conn, cid, status="open", payload_merge=payload)

    return {"candidate_id": cid, "task_id": tid, "deduped": False}


def record_freeform_connector_intent(conn, data_dir: Path, *, source_text: str) -> Dict[str, Any]:
    """User-typed 'connect Gmail' etc. during onboarding."""
    text = (source_text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}

    resource_id = "custom"
    lowered = text.casefold()
    for res in RESOURCE_CATALOG:
        if res["resource_id"] in lowered or res["label"].casefold() in lowered:
            resource_id = res["resource_id"]
            break

    out = create_connector_intent(
        conn,
        resource_id=resource_id,
        user_note=text,
        source="onboarding_chat",
        custom_label=text[:120] if resource_id == "custom" else "",
    )
    from preference_promotion import record_explicit_preference

    pref = record_explicit_preference(
        conn,
        text=f"Wants to connect: {text}",
        source="connector_intent",
        auto_activate=False,
    )
    return {"ok": True, **out, "preference": pref}


def list_open_connector_work(conn, *, limit: int = 8) -> List[Dict[str, Any]]:
    from store import graph_candidate_list, task_list

    out: List[Dict[str, Any]] = []
    for c in graph_candidate_list(conn, status="open", limit=limit * 3):
        if c.get("candidate_type") != "connector_intent":
            continue
        payload = c.get("payload") or {}
        out.append(
            {
                "candidate_id": c.get("candidate_id"),
                "title": c.get("title"),
                "resource_id": payload.get("resource_id"),
                "import_hint": payload.get("import_hint"),
                "task_id": payload.get("task_id"),
            }
        )
        if len(out) >= limit:
            break
    return out


def poll_questions_for_llm(data_dir: Path) -> List[Dict[str, str]]:
    """Compact list for onboarding LLM context."""
    state = load_resource_poll(data_dir)
    answers = state.get("answers") or {}
    qs: List[Dict[str, str]] = []
    for res in RESOURCE_CATALOG:
        rid = res["resource_id"]
        ans = answers.get(rid)
        qs.append(
            {
                "resource_id": rid,
                "question": res["question"],
                "answered": ans is not None,
                "uses": ans.get("uses") if isinstance(ans, dict) else None,
            }
        )
    return qs
