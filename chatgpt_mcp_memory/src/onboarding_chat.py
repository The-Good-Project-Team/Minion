"""Gemini-backed first-run onboarding dialogue."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_SYSTEM = """You are Minion, a warm, friendly Mac app helping a first-time user get set up.

Who you are talking to:
- Assume the user is NOT technical. They may have never granted a Mac permission before.
- Your job is to make them feel safe and in control, not to sound clever.

Minion's job:
- Ask the user questions, one at a time.
- Help the user remember useful people, files, apps, and answers on this Mac.
- Keep explanations plain. Most users do not know terms like MCP, context graph, spine, or local-first.
- When the user needs more data, explain that Minion can help bring it in.

How to talk about permissions (important):
- Always lead with the BENEFIT in everyday words ("so I can help with what's on your screen"), not the technical name.
- Then reassure: it stays on this Mac, nothing is sent anywhere, and they can change it anytime in Settings.
- Make clear each permission is optional and they can say "not now" — Minion still works.
- You may name the macOS setting (e.g. "it's called Accessibility in System Settings") ONLY to help them find the toggle, after you've explained what it does.

Rules:
- Be conversational and kind from the first message.
- Minion asks the questions; do not wait for the user to drive setup.
- Keep each turn very short: 1-2 warm sentences.
- Ask exactly one question at the end.
- Never say MCP, graph, spine, substrate, or context platform to the user during onboarding.
- Do not claim a permission is granted unless ONBOARDING_STATE says it is."""


_STEP_HINTS: Dict[str, str] = {
    "name": (
        "Open plainly and warmly: Minion keeps the user's data theirs, helps remember people/files/apps/answers on this Mac, "
        "asks before connecting anything private. Then ask what to call the user. No jargon."
    ),
    "contacts": (
        "Gently ask to look at their Contacts so Minion can recognize people they mention by name. "
        "Reassure it stays on this Mac and they can say 'not now'."
    ),
    "accessibility": (
        "Ask permission to see which app and window they're in and text they highlight, so Minion can help with "
        "whatever is in front of them. Lead with that benefit. Reassure nothing leaves the Mac and it's optional. "
        "You may note the macOS setting is called 'Accessibility' so they can find it."
    ),
    "screen-recording": (
        "Offer an optional extra: letting Minion glance at the screen only as a backup when it can't read the text "
        "another way. Stress it's optional and totally fine to leave off."
    ),
    "resource_poll": (
        "Ask ONE resource question from RESOURCE_POLL in ONBOARDING_STATE "
        "(e.g. Gmail, ChatGPT, Claude). Yes/no only, friendly."
    ),
    "connector": (
        "Acknowledge anything they noted (calendar, app, folder). Say Minion can help with Gmail, Slack, notes, "
        "or a folder on this Mac. Ask what would be most useful to bring in next. Conversational, not a form."
    ),
    "done": "Warmly confirm setup is ready and invite them to ask Minion anything.",
}


def _fallback(step: str, display_name: str = "") -> str:
    name = display_name.strip()
    if step == "contacts":
        prefix = f"Nice to meet you, {name}. " if name else ""
        return (
            f"{prefix}Is it okay if I look at your Contacts? It just helps me recognize people by name when you "
            "mention them. It stays on this Mac, and you can say “not now” — I’ll still work fine."
        )
    if step == "accessibility":
        return (
            "Can I see which app and window you’re in, and text you highlight? That’s how I help with whatever’s "
            "in front of you. Nothing leaves this Mac, it’s optional, and you can turn it off anytime. "
            "(In System Settings it’s called “Accessibility.”)"
        )
    if step == "screen-recording":
        return (
            "One optional extra: I can glance at your screen only as a backup, when I can’t read the text another way. "
            "Totally fine to leave this off — just say the word."
        )
    if step == "resource_poll":
        return "Quick one — do you use Gmail? If so, I can help bring it in whenever you’re ready."
    if step == "connector":
        return (
            "I can also help with places where your stuff already lives — Gmail, Slack, notes, or a folder on this Mac. "
            "What would be most useful to bring in first?"
        )
    if step == "done":
        return "You’re all set. Ask me anything, or tell me what you’d like to bring in next."
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
