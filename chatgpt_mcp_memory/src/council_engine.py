"""Orchestrate pattern evaluation → proposals → feed items."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from council_skills import approval_options_for_skill, get_skill
from council_store import (
    council_event_insert,
    council_open_subject_ids,
    council_pattern_state_is_suppressed,
    council_proposal_upsert_open,
    council_proposals_expire_stale,
)
from leverage_patterns import EventContext, PatternSpec, list_patterns
from corpus_context import prefetch_for_subject
from proposal_builder import ProposalBuildContext, build_payload, build_title_summary
from required_info import gate_intensity, resolve_required_info

log = logging.getLogger(__name__)

_MAX_ELEVATED = 1
_MAX_STANDARD = 2


def evaluate_patterns(
    conn,
    data_dir,
    *,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Run detectors, upsert proposals, return feed-shaped council items."""
    now = now or time.time()
    council_proposals_expire_stale(conn)
    fired: List[tuple[PatternSpec, EventContext, str]] = []

    for pattern in list_patterns(enabled_only=True):
        try:
            ctx = pattern.detect(conn, data_dir, now=now)
        except Exception:
            log.exception("pattern detect failed: %s", pattern.pattern_id)
            continue
        if ctx is None:
            continue
        if council_pattern_state_is_suppressed(
            conn, pattern.pattern_id, ctx.subject_id, now=now
        ):
            continue
        fired.append((pattern, ctx, pattern.pattern_id))

    fired.sort(key=lambda x: (-x[0].proposal_spec.leverage_weight, x[1].subject_id))
    elevated = 0
    standard = 0
    items: List[Dict[str, Any]] = []

    for pattern, ctx, _ in fired:
        ps = pattern.proposal_spec
        if ps.intensity == "elevated" and elevated >= _MAX_ELEVATED:
            continue
        if ps.intensity != "elevated" and standard >= _MAX_STANDARD:
            continue

        skill = get_skill(ps.skill_id)
        keys = skill.required_info_keys if skill else ()
        req_info = resolve_required_info(
            conn, keys, subject_id=ctx.subject_id, data_dir=data_dir
        )
        eff_intensity, surface = gate_intensity(ps.intensity, req_info, ps.skill_id)
        if not surface:
            _telemetry_deferred(pattern.pattern_id, ctx.subject_id, req_info)
            continue

        if eff_intensity == "elevated":
            if elevated >= _MAX_ELEVATED:
                continue
        else:
            if standard >= _MAX_STANDARD:
                continue

        corpus = prefetch_for_subject(
            conn,
            subject_label=ctx.subject_label,
            subject_id=ctx.subject_id,
            top_k=5,
        )
        evidence = list(ctx.evidence_refs) + [
            r for r in corpus.get("evidence_refs", []) if r not in ctx.evidence_refs
        ]
        wiki = str(corpus.get("wiki_excerpt") or "")
        if not corpus.get("hits") and len(wiki) < 8:
            _telemetry_deferred(pattern.pattern_id, ctx.subject_id, {"reason": "no_corpus"})
            continue

        bctx = ProposalBuildContext(
            event_type=pattern.event_type,
            subject_id=ctx.subject_id,
            subject_label=ctx.subject_label,
            pattern_id=pattern.pattern_id,
            evidence_refs=evidence,
            meta=ctx.meta,
            corpus=corpus,
        )
        payload = build_payload(ps.proposal_type, bctx)
        title, summary = build_title_summary(ps.proposal_type, bctx)

        event_id = council_event_insert(
            conn,
            event_type=pattern.event_type,
            domain=pattern.domain,
            subject_id=ctx.subject_id,
            pattern_id=pattern.pattern_id,
            evidence_refs=evidence,
            detected_at=now,
        )
        proposal_id = council_proposal_upsert_open(
            conn,
            event_id=event_id,
            pattern_id=pattern.pattern_id,
            subject_id=ctx.subject_id,
            proposal_type=ps.proposal_type,
            title=title,
            summary=summary,
            payload=payload,
            intensity=eff_intensity,
            required_skill=ps.skill_id,
            required_info=req_info,
        )
        _telemetry_fired(pattern.pattern_id, proposal_id)
        if eff_intensity == "elevated":
            elevated += 1
        else:
            standard += 1
        items.append(
            proposal_to_feed_item(
                conn,
                proposal_id,
                pattern_id=pattern.pattern_id,
                event_type=pattern.event_type,
                domain=pattern.domain,
                evidence_refs=ctx.evidence_refs,
            )
        )

    return items


def proposal_to_feed_item(
    conn,
    proposal_id: str,
    *,
    pattern_id: str = "",
    event_type: str = "",
    domain: str = "",
    evidence_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from council_store import council_proposal_get

    prop = council_proposal_get(conn, proposal_id)
    if not prop:
        return {}
    skill_id = prop["required_skill"]
    return {
        "item_kind": "council",
        "ts": prop["updated_at"],
        "event": {
            "event_type": event_type or "council",
            "subject_id": prop["subject_id"],
            "domain": domain or "relationships",
            "evidence_refs": evidence_refs or [],
            "pattern_id": pattern_id or prop.get("pattern_id"),
        },
        "proposal": {
            "proposal_id": prop["proposal_id"],
            "proposal_type": prop["proposal_type"],
            "title": prop["title"],
            "summary": prop["summary"],
            "payload": prop["payload"],
            "intensity": prop["intensity"],
        },
        "required_skill": skill_id,
        "required_info": prop["required_info"],
        "approval": {"options": approval_options_for_skill(skill_id)},
    }


def list_open_feed_items(conn, data_dir) -> List[Dict[str, Any]]:
    from council_store import council_proposals_list_open

    props = council_proposals_list_open(conn, limit=10)
    items = []
    for p in props:
        items.append(
            proposal_to_feed_item(
                conn,
                p["proposal_id"],
                pattern_id=p.get("pattern_id", ""),
                event_type="",
                domain="relationships",
            )
        )
    items.sort(
        key=lambda x: (0 if x.get("proposal", {}).get("intensity") == "elevated" else 1, -x["ts"])
    )
    return items


def council_subject_ids_with_open(conn) -> set[str]:
    return council_open_subject_ids(conn)


def _telemetry_fired(pattern_id: str, proposal_id: str) -> None:
    try:
        from telemetry import log_event

        log_event("council_pattern_fired", pattern_id=pattern_id, proposal_id=proposal_id)
    except Exception:
        pass


def _telemetry_deferred(pattern_id: str, subject_id: str, req_info: Dict[str, Any]) -> None:
    try:
        from telemetry import log_event

        log_event(
            "council_gate_deferred",
            pattern_id=pattern_id,
            subject_id=subject_id,
            required_info={k: v.get("status") for k, v in req_info.items()},
        )
    except Exception:
        pass
