"""Gemini-backed 42 dialogue on top of deterministic graph_fill."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)

_SYSTEM = """You are 42, Minion's guide for filling in the user's private life graph (people, projects, places, work).

Rules:
- Keep replies short: one or two paragraphs, conversational, warm but direct.
- You may use light markdown (**bold** for names). Do not prefix lines with "**42:**".
- Use ONLY facts from GRAPH_ACTION and THREAD — never invent people, jobs, or relationships.
- If the user greeted you or gave filler, acknowledge it and ask clearly for what you need (a name, how they know someone, yes/no on a claim, or skip).
- If GRAPH_ACTION says the graph was updated, confirm what changed in plain language.
- If GRAPH_ACTION includes a follow-up question, ask it naturally — do not repeat canned boilerplate about "the next empty spot".
- If they should tap a contact chip, mention that briefly when suggestions are listed.
- Do not lecture about privacy or how Minion works unless they asked."""

_SYSTEM_OPENING = """You are 42, Minion's guide building the user's private life graph.

Given MINION_CONTEXT JSON, ask ONE clear question to fill the highest-priority gap.
- Use capture/corpus hints when relevant (what they're looking at now).
- For empty buckets, suggest contact names if listed — invite tap or type.
- Short, warm, one paragraph. Light markdown ok. No "**42:**" prefix.
- Never invent facts not in MINION_CONTEXT."""

_SYSTEM_PARSE = """You extract what the user meant for their life graph.

Given MINION_CONTEXT, THREAD, and USER_MESSAGE, return ONLY valid JSON:
{
  "intent": "answer" | "dismiss" | "skip",
  "answer_text": "normalized factual answer for graph logic, or empty",
  "extracted_name": "person/project name if they named someone, else null",
  "notes": "brief reason"
}

- intent dismiss/skip for "later", "not now", "skip", off-topic refusal
- For greetings with no name, intent answer with empty answer_text
- extracted_name only when clearly naming a person/project/place
- Do not invent names"""


def _thread_messages(thread: Dict[str, Any], *, max_msgs: int = 14) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in (thread.get("messages") or [])[-max_msgs:]:
        role = str(m.get("role") or "user")
        body = str(m.get("body_md") or "").strip()
        if body:
            out.append({"role": role, "content": body})
    return out


def _action_context(
    conn,
    thread: Dict[str, Any],
    result: Dict[str, Any],
    *,
    user_text: str,
    data_dir: Optional[Path] = None,
) -> str:
    gap = (thread.get("meta") or {}).get("gap") or {}
    suggestions = []
    for m in reversed(thread.get("messages") or []):
        if m.get("role") == "assistant":
            suggestions = list((m.get("meta") or {}).get("suggestions") or [])
            break
    minion_ctx: Dict[str, Any] = {}
    if conn is not None:
        try:
            from forty_two_context import build_minion_context

            label = str(gap.get("label") or gap.get("bucket_label") or "")
            minion_ctx = build_minion_context(
                conn,
                data_dir=data_dir,
                subject_label=label,
            )
        except Exception:
            minion_ctx = {}

    payload = {
        "minion_context": minion_ctx,
        "gap": gap,
        "graph_action": {
            "deltas": result.get("deltas"),
            "resolved": result.get("resolved"),
            "follow_up_template": result.get("follow_up"),
            "error": result.get("error"),
        },
        "contact_suggestions": [s.get("name") for s in suggestions if s.get("name")],
        "last_user_message": user_text,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def compose_opening_question(
    conn,
    gap: Dict[str, Any],
    *,
    data_dir: Optional[Path] = None,
) -> Tuple[str, bool]:
    """LLM opening line for a new gap thread."""
    from gemini_client import forty_two_gemini_model, gemini_chat, gemini_configured
    from graph_fill import compose_question
    from forty_two_context import build_minion_context, context_markdown

    fallback = compose_question(conn, gap, data_dir=data_dir)
    if not gemini_configured(data_dir):
        return fallback, False

    label = str(gap.get("label") or gap.get("bucket_label") or "")
    ctx = build_minion_context(conn, data_dir=data_dir, subject_label=label)
    if gap.get("suggestions"):
        ctx["contact_suggestions"] = gap["suggestions"]

    user = f"MINION_CONTEXT:\n{context_markdown(ctx)}\n\nWrite your question."
    try:
        text = gemini_chat(
            system=_SYSTEM_OPENING,
            messages=[{"role": "user", "content": user}],
            data_dir=data_dir,
            model=forty_two_gemini_model(data_dir),
            temperature=0.55,
            max_output_tokens=512,
        )
        return text.strip(), True
    except Exception as exc:
        log.warning("42 gemini opening failed: %s", exc)
        return fallback, False


def normalize_user_turn(
    conn,
    thread: Dict[str, Any],
    text: str,
    *,
    data_dir: Optional[Path] = None,
) -> Tuple[str, Optional[str]]:
    """Return (body_for_apply_answer, action_override)."""
    from gemini_client import forty_two_gemini_model, gemini_chat, gemini_configured
    from forty_two_context import build_minion_context, context_markdown

    raw = (text or "").strip()
    if not raw or not gemini_configured(data_dir):
        return raw, None

    gap = (thread.get("meta") or {}).get("gap") or {}
    label = str(gap.get("label") or gap.get("bucket_label") or "")
    ctx = build_minion_context(conn, data_dir=data_dir, subject_label=label)
    user = (
        f"MINION_CONTEXT:\n{context_markdown(ctx)}\n\n"
        f"THREAD gap:\n{json.dumps(gap, ensure_ascii=False)}\n\n"
        f"USER_MESSAGE:\n{raw}"
    )
    try:
        out = gemini_chat(
            system=_SYSTEM_PARSE,
            messages=[{"role": "user", "content": user}],
            data_dir=data_dir,
            model=forty_two_gemini_model(data_dir),
            temperature=0.1,
            max_output_tokens=256,
        )
        raw_json = out.strip()
        if "```" in raw_json:
            raw_json = raw_json.split("```", 2)[1]
            if raw_json.lower().startswith("json"):
                raw_json = raw_json[4:]
        parsed = json.loads(raw_json.strip())
        intent = str(parsed.get("intent") or "answer")
        if intent in ("dismiss", "skip"):
            return raw, "dismiss"
        name = parsed.get("extracted_name")
        answer = str(parsed.get("answer_text") or "").strip()
        if name and isinstance(name, str):
            return name.strip()[:200], None
        if answer:
            return answer[:2000], None
        return raw, None
    except Exception as exc:
        log.debug("42 parse turn skipped: %s", exc)
        return raw, None


def compose_42_reply(
    conn,
    thread: Dict[str, Any],
    result: Dict[str, Any],
    *,
    user_text: str,
    data_dir: Optional[Path] = None,
) -> Tuple[str, bool]:
    """Return (body_md, used_gemini)."""
    from gemini_client import forty_two_gemini_model, gemini_chat, gemini_configured
    from graph_fill import format_confirmation

    fallback = format_confirmation(result)
    if not gemini_configured(data_dir):
        return fallback, False

    messages = _thread_messages(thread)
    if not any(m["role"] == "user" and m["content"] == user_text for m in messages):
        messages.append({"role": "user", "content": user_text})

    user_block = (
        "GRAPH_ACTION and context (JSON):\n"
        f"{_action_context(conn, thread, result, user_text=user_text, data_dir=data_dir)}\n\n"
        "Continue the conversation as 42."
    )
    messages.append({"role": "user", "content": user_block})

    try:
        text = gemini_chat(
            system=_SYSTEM,
            messages=messages,
            data_dir=data_dir,
            model=forty_two_gemini_model(data_dir),
            max_output_tokens=768,
        )
        return text.strip(), True
    except Exception as exc:
        log.warning("42 gemini reply failed: %s", exc)
        return fallback, False


def iter_42_reply_deltas(
    conn,
    thread: Dict[str, Any],
    result: Dict[str, Any],
    *,
    user_text: str,
    data_dir: Optional[Path] = None,
) -> Tuple[Iterator[str], List[bool]]:
    """Return (text chunks, single-element list set True when Gemini produced tokens)."""
    from chat_sse import iter_text_deltas
    from gemini_client import forty_two_gemini_model, gemini_chat_stream, gemini_configured
    from graph_fill import format_confirmation

    fallback = format_confirmation(result)
    used: List[bool] = [False]
    if not gemini_configured(data_dir):
        return iter_text_deltas(fallback), used

    messages = _thread_messages(thread)
    if not any(m["role"] == "user" and m["content"] == user_text for m in messages):
        messages.append({"role": "user", "content": user_text})
    user_block = (
        "GRAPH_ACTION and context (JSON):\n"
        f"{_action_context(conn, thread, result, user_text=user_text, data_dir=data_dir)}\n\n"
        "Continue the conversation as 42."
    )
    messages.append({"role": "user", "content": user_block})

    def _gemini() -> Iterator[str]:
        try:
            got = False
            for piece in gemini_chat_stream(
                system=_SYSTEM,
                messages=messages,
                data_dir=data_dir,
                model=forty_two_gemini_model(data_dir),
                max_output_tokens=768,
            ):
                got = True
                used[0] = True
                yield piece
            if not got:
                yield from iter_text_deltas(fallback)
        except Exception as exc:
            log.warning("42 gemini stream failed: %s", exc)
            yield from iter_text_deltas(fallback)

    return _gemini(), used


_SYSTEM_INFER = """You propose graph writes for Minion's private life graph from EVIDENCE only.

Given GAP (what is missing), EVIDENCE (search hits from the user's indexed notes), and optional GRAPH_CONTEXT,
return ONLY valid JSON:
{
  "confidence": 0.0 to 1.0,
  "actions": [
    {
      "type": "set_person_summary" | "set_me_profile" | "add_edge" | "create_node" | "approve_claim" | "reject_claim",
      "node_id": "existing graph node_id when applicable",
      "to_node_id": "for add_edge",
      "from_node_id": "scaffold-me default",
      "rel_kind": "knows | related_to | belongs_to | works_at | ...",
      "node_kind": "person | project | place | organization | group",
      "title": "for create_node",
      "parent_node_id": "scaffold bucket id when creating",
      "summary": "one-line factual summary",
      "relation_note": "how user knows them",
      "user_note": "short note",
      "claim_id": "for claim gaps",
      "evidence_refs": ["chunk:...", "graph:..."]
    }
  ],
  "unresolved_question": "one disambiguating question if evidence conflicts, else empty string",
  "reasoning_notes": "brief"
}

Rules:
- Use ONLY facts supported by EVIDENCE snippets. Every action MUST cite chunk: IDs from hits.
- confidence >= 0.85 only when multiple hits agree; else lower and set unresolved_question.
- For person gap: prefer set_person_summary with node_id from GAP.subject_id.
- For person_relation: prefer add_edge knows from scaffold-me to subject_id.
- For bucket: prefer create_node with plausible title from evidence.
- For claim: approve_claim or reject_claim with claim_id from GAP.
- Never invent names not appearing in evidence.
- If evidence is thin or conflicting, set unresolved_question and confidence < 0.75."""

_SYSTEM_MINE_DURABLE = """You mine durable life-graph facts from EVIDENCE — family, birthplace, home, employers, biography.

Same JSON schema as standard infer, plus action type set_me_profile for scaffold-me (user biography).

Rules:
- Prefer create_node under the GAP parent bucket for family members, home places, employers.
- Use parent_of, child_of, married_to, lives_at, works_at when evidence supports structure.
- set_me_profile: one consolidated biography paragraph for Me (where from, role, key facts).
- stability is "core" — only facts likely true for years, not this week's task.
- Every action MUST cite chunk: IDs. Never invent names.
- Return ONE JSON object; never a bare JSON array."""

_SYSTEM_MINE_ONGOING = """You maintain an ongoing life graph from EVIDENCE — people, projects, roles, updates.

Same JSON schema. Focus on GAP: fill missing summaries, add nodes to empty buckets, enrich thin nodes.

Rules:
- create_node for friends, active projects, work contacts when names appear in evidence.
- set_person_summary to enrich existing nodes (GAP.enrich or person gaps).
- add_edge knows / works_at / participates_in / responsible_for when clear.
- stability is "active" — current work and relationships; ok to update as context shifts.
- Every action MUST cite chunk: IDs. Never invent names.
- Return ONE JSON object with keys confidence, actions, unresolved_question, reasoning_notes.
- Never return a bare JSON array. If no actions, use "actions": []."""


MINING_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "confidence": {"type": "number"},
        "actions": {
            "type": "array",
            "items": {"type": "object"},
        },
        "unresolved_question": {"type": "string"},
        "reasoning_notes": {"type": "string"},
    },
    "required": ["confidence", "actions"],
}


def _parse_proposal_json(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None
    if "```" in text:
        text = text.split("```", 2)[1]
        if text.lower().startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        return {"confidence": 0.0, "actions": [], "unresolved_question": "", "reasoning_notes": ""}
    if isinstance(parsed, dict):
        parsed.setdefault("confidence", 0.0)
        parsed.setdefault("actions", [])
        parsed.setdefault("unresolved_question", "")
        parsed.setdefault("reasoning_notes", "")
        return parsed
    return None


def _infer_system(mining_kind: str) -> str:
    if mining_kind == "durable":
        return _SYSTEM_MINE_DURABLE
    if mining_kind == "ongoing":
        return _SYSTEM_MINE_ONGOING
    return _SYSTEM_INFER


def propose_graph_actions_from_evidence(
    conn,
    gap: Dict[str, Any],
    evidence_pack: Dict[str, Any],
    *,
    data_dir: Optional[Path] = None,
    mining_kind: str = "ongoing",
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """LLM proposes structured graph actions from retrieval pack."""
    import os
    from pathlib import Path as _Path

    from gemini_client import gemini_chat, gemini_configured, graph_mine_gemini_model
    from forty_two_context import build_mining_context, mining_context_markdown

    if not gemini_configured(data_dir):
        return None, False

    hits = evidence_pack.get("hits") or []
    if not hits:
        return None, False

    ctx = build_mining_context(conn, gap=gap)
    evidence_block = json.dumps(
        {
            "hits": [
                {
                    "chunk_id": h.get("chunk_id"),
                    "score": h.get("score"),
                    "path": os.path.basename(str(h.get("path") or "note")),
                    "text": (h.get("text") or "")[:400],
                }
                for h in hits[:10]
            ],
            "evidence_refs": evidence_pack.get("evidence_refs"),
        },
        ensure_ascii=False,
        indent=2,
    )
    user = (
        f"GRAPH_CONTEXT:\n{mining_context_markdown(ctx)}\n\n"
        f"MINING_KIND: {mining_kind}\n"
        f"STABILITY: {gap.get('stability') or ('core' if mining_kind == 'durable' else 'active')}\n\n"
        f"GAP:\n{json.dumps(gap, ensure_ascii=False, indent=2)}\n\n"
        f"EVIDENCE:\n{evidence_block}\n\n"
        "Return one JSON object (never a bare array). Extract durable facts supported by EVIDENCE."
    )
    try:
        out = gemini_chat(
            system=_infer_system(mining_kind),
            messages=[{"role": "user", "content": user}],
            data_dir=_Path(data_dir) if data_dir else None,
            model=graph_mine_gemini_model(_Path(data_dir) if data_dir else None),
            temperature=0.15,
            max_output_tokens=8192,
            timeout_seconds=90.0,
            response_mime_type="application/json",
            response_schema=MINING_RESPONSE_SCHEMA,
        )
        parsed = _parse_proposal_json(out)
        if not parsed:
            log.warning("42 corpus infer returned unparsable JSON: %r", out[:200])
            return None, True
        if not parsed.get("actions"):
            log.info(
                "42 corpus infer found no cited actions for gap=%s",
                gap.get("gap_type") or gap.get("label"),
            )
            return None, True
        parsed["evidence_refs"] = list(evidence_pack.get("evidence_refs") or [])
        for act in parsed.get("actions") or []:
            if not act.get("evidence_refs"):
                act["evidence_refs"] = [
                    f"chunk:{h['chunk_id']}"
                    for h in hits[:3]
                    if h.get("chunk_id")
                ]
        return parsed, True
    except Exception as exc:
        log.warning("42 corpus infer LLM failed: %s", exc)
        return None, True
