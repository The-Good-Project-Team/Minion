"""Gemini-backed first-run onboarding dialogue."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_SYSTEM = """You are Minion, a friendly Mac app onboarding the user.

Minion's job:
- Ask the user questions, one at a time.
- Help the user remember useful people, files, apps, and answers on this Mac.
- Keep explanations plain. Most users do not know terms like MCP, context graph, spine, or local-first.
- When the user needs more data, explain that Minion can write or guide small connectors to import it.

Rules:
- Be conversational from the first message.
- Minion asks the questions; do not wait for the user to drive setup.
- Keep each turn very short: 1-2 sentences.
- Ask exactly one question at the end.
- Explain permissions plainly: what it enables, why it helps, and that it stays on this Mac.
- Never say MCP, graph, spine, substrate, or context platform to the user during onboarding.
- Do not claim a permission is granted unless ONBOARDING_STATE says it is."""


_STEP_HINTS: Dict[str, str] = {
    "name": (
        "Open plainly: Minion keeps the user's data theirs, helps remember people/files/apps/answers on this Mac, "
        "asks before connecting anything private. Then ask what to call the user. No jargon."
    ),
    "contacts": "Ask for Contacts permission. Explain Minion uses names to recognize people the user mentions.",
    "accessibility": "Ask for Accessibility permission. Explain it helps Minion understand active app/window context and selected text.",
    "screen-recording": "Ask for optional Screen Recording. Explain it is a fallback when app text is unavailable.",
    "resource_poll": (
        "Ask ONE resource question from RESOURCE_POLL in ONBOARDING_STATE "
        "(e.g. Gmail, ChatGPT, Claude). Yes/no only."
    ),
    "connector": (
        "Acknowledge anything they noted (calendar, app, folder). Say Minion can help with Gmail, Slack, notes, "
        "or a folder on this Mac. Ask what would be most useful to connect next. Conversational, not a form."
    ),
    "done": "Confirm setup is ready and invite them to ask Minion anything.",
}


def _fallback(step: str, display_name: str = "") -> str:
    name = display_name.strip()
    if step == "contacts":
        prefix = f"Nice to meet you, {name}. " if name else ""
        return (
            f"{prefix}Can I read your Contacts? I use names from your address book to recognize people "
            "you mention later. This stays on this Mac."
        )
    if step == "accessibility":
        return (
            "Next, can I use Accessibility? It lets me understand the active app, window, and selected text "
            "when you want context, without sending that context anywhere by default."
        )
    if step == "screen-recording":
        return (
            "One optional permission: can I use Screen Recording as a fallback? It helps when app text is unavailable, "
            "and you can leave it off if you only want text-based context."
        )
    if step == "resource_poll":
        return "Quick check — do you use Gmail? If yes, I can help bring it in when you're ready."
    if step == "connector":
        return (
            "I can also help with places where your work already lives, like Gmail, Slack, notes, "
            "or a folder on this Mac. What would be most useful to connect next?"
        )
    if step == "done":
        return "You're all set. Ask me anything, or tell me what to connect next."
    return (
        "I keep your data yours.\n"
        "I help you remember useful people, files, apps, and answers on this Mac. "
        "I'll ask before connecting anything private.\n"
        "What should I call you?"
    )


def onboarding_reply(
    *,
    step: str,
    display_name: str = "",
    transcript: Optional[List[Dict[str, str]]] = None,
    data_dir: Optional[Path] = None,
    permission_status: Optional[Dict[str, str]] = None,
) -> Tuple[str, bool]:
    """Return (assistant_text, used_llm)."""
    safe_step = step if step in _STEP_HINTS else "name"
    fallback = _fallback(safe_step, display_name)

    try:
        from gemini_client import gemini_chat, gemini_configured, gemini_model

        if not gemini_configured(data_dir):
            return fallback, False
        poll_ctx: Dict[str, Any] = {}
        if data_dir:
            try:
                from connector_intent import next_poll_question, poll_questions_for_llm

                poll_ctx = {
                    "RESOURCE_POLL": poll_questions_for_llm(Path(data_dir)),
                    "NEXT_POLL": next_poll_question(Path(data_dir)),
                }
            except Exception:
                poll_ctx = {}
        state = {
            "step": safe_step,
            "display_name": display_name,
            "step_instruction": _STEP_HINTS[safe_step],
            "permission_status": dict(permission_status or {}),
            **poll_ctx,
        }
        messages = list(transcript or [])[-10:]
        messages.append({"role": "user", "content": f"ONBOARDING_STATE: {state!r}\nWrite Minion's next turn."})
        text = gemini_chat(
            system=_SYSTEM,
            messages=messages,
            data_dir=data_dir,
            model=gemini_model(data_dir),
            temperature=0.7,
            max_output_tokens=256,
            timeout_seconds=45,
        )
        return text.strip() or fallback, True
    except Exception as exc:
        log.warning("onboarding gemini failed: %s", exc)
        return fallback, False
