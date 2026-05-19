"""Build proposal payloads per proposal_type."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from corpus_context import corpus_summary_line

BuilderFn = Callable[["ProposalBuildContext"], Dict[str, Any]]


@dataclass
class ProposalBuildContext:
    event_type: str
    subject_id: str
    subject_label: str
    pattern_id: str
    evidence_refs: list
    meta: Dict[str, Any]
    corpus: Optional[Dict[str, Any]] = None


_BUILDERS: Dict[str, BuilderFn] = {}


def register_builder(proposal_type: str, fn: BuilderFn) -> None:
    _BUILDERS[proposal_type] = fn


def build_payload(proposal_type: str, ctx: ProposalBuildContext) -> Dict[str, Any]:
    fn = _BUILDERS.get(proposal_type)
    if fn:
        return fn(ctx)
    return {"proposal_type": proposal_type, "subject_id": ctx.subject_id}


def build_title_summary(
    proposal_type: str, ctx: ProposalBuildContext
) -> tuple[str, str]:
    if proposal_type == "outbound_message":
        days = ctx.meta.get("days_since_contact")
        summary = f"No contact in {int(days)} days" if days is not None else "Time to check in"
        if ctx.corpus:
            cite = corpus_summary_line(ctx.corpus)
            if cite:
                summary = f"{summary}. {cite}"
        return f"Check in with {ctx.subject_label}", summary
    if proposal_type == "commerce_action":
        horizon = ctx.meta.get("horizon_days", "?")
        return (
            f"{ctx.subject_label}: date in {horizon} days — starter gesture?",
            "Relationship tier supports a small gesture when you're ready.",
        )
    if proposal_type == "calendar_hold":
        return (
            f"Follow up with {ctx.subject_label}",
            "Schedule a short hold after first meeting.",
        )
    return (f"Proposal for {ctx.subject_label}", "")


def _outbound_message(ctx: ProposalBuildContext) -> Dict[str, Any]:
    body = ctx.meta.get("draft_body") or f"Hey {ctx.subject_label} — thinking of you. How have you been?"
    return {
        "channel": ctx.meta.get("channel", "imessage"),
        "body": body,
        "subject_id": ctx.subject_id,
    }


def _commerce_action(ctx: ProposalBuildContext) -> Dict[str, Any]:
    return {
        "line_items": ctx.meta.get(
            "line_items",
            [{"name": "Starter bouquet", "qty": 1, "estimate_usd": 45}],
        ),
        "fulfillment": ctx.meta.get("fulfillment", {"mode": "delivery"}),
        "payment_ref": ctx.meta.get("payment_ref"),
        "subject_id": ctx.subject_id,
    }


def _calendar_hold(ctx: ProposalBuildContext) -> Dict[str, Any]:
    return {
        "title": f"Follow up: {ctx.subject_label}",
        "duration_minutes": 30,
        "subject_id": ctx.subject_id,
    }


register_builder("outbound_message", _outbound_message)
register_builder("commerce_action", _commerce_action)
register_builder("calendar_hold", _calendar_hold)
