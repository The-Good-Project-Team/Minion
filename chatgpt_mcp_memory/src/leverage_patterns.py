"""Declarative leverage pattern registry (detectors → proposal specs)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

DetectFn = Callable[..., Optional["EventContext"]]


@dataclass
class ProposalSpec:
    proposal_type: str
    skill_id: str
    intensity: str = "standard"
    leverage_weight: float = 1.0


@dataclass
class PatternSpec:
    pattern_id: str
    domain: str
    event_type: str
    proposal_spec: ProposalSpec
    detect: DetectFn
    enabled: bool = True
    min_cadence_days: float = 14.0


@dataclass
class EventContext:
    subject_id: str
    subject_label: str
    evidence_refs: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


_REGISTRY: List[PatternSpec] = []


def register_pattern(spec: PatternSpec) -> None:
    _REGISTRY.append(spec)


def list_patterns(*, enabled_only: bool = True) -> List[PatternSpec]:
    if enabled_only:
        return [p for p in _REGISTRY if p.enabled]
    return list(_REGISTRY)


def detect_contact_drift(conn, data_dir, *, now: Optional[float] = None) -> Optional[EventContext]:
    now = now or time.time()
    threshold_days = 21.0
    rows = conn.execute(
        "SELECT node_id, title, summary, body_md, updated_at FROM graph_nodes "
        "WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub') "
        "AND parent_node_id='scaffold-people-friends'"
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT node_id, title, summary, body_md, updated_at FROM graph_nodes "
            "WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
        ).fetchall()
    best: Optional[EventContext] = None
    best_gap = 0.0
    for row in rows:
        meta = _parse_meta(row)
        last = meta.get("last_contact_at")
        if last is None:
            gap_days = threshold_days + 7.0
        else:
            gap_days = (now - float(last)) / 86400.0
        if gap_days < threshold_days:
            continue
        label = str(row["title"] or "Someone")
        ctx = EventContext(
            subject_id=str(row["node_id"]),
            subject_label=label,
            evidence_refs=[f"graph:{row['node_id']}"],
            meta={"days_since_contact": int(gap_days), "channel": "imessage"},
        )
        if gap_days > best_gap:
            best_gap = gap_days
            best = ctx
    return best


def _parse_meta(row: Any) -> Dict[str, Any]:
    import json

    for field in ("summary", "body_md"):
        raw = row[field] if field in row.keys() else None
        if not raw:
            continue
        s = str(raw).strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
    return {}


def detect_date_horizon(conn, data_dir, *, now: Optional[float] = None) -> Optional[EventContext]:
    now = now or time.time()
    rows = conn.execute(
        "SELECT node_id, title, summary, body_md FROM graph_nodes "
        "WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    for row in rows:
        meta = _parse_meta(row)
        bday = meta.get("birthday")
        if not bday:
            continue
        try:
            parts = str(bday).split("-")
            month, day = int(parts[-2]), int(parts[-1])
        except (ValueError, IndexError):
            continue
        import datetime as dt

        today = dt.datetime.utcfromtimestamp(now)
        year = today.year
        try:
            target = dt.datetime(year, month, day)
        except ValueError:
            continue
        if target.timestamp() < now:
            try:
                target = dt.datetime(year + 1, month, day)
            except ValueError:
                continue
        horizon = (target.timestamp() - now) / 86400.0
        if horizon < 0 or horizon > 7:
            continue
        tier = _relationship_tier(conn, str(row["node_id"]))
        if tier < 2:
            continue
        return EventContext(
            subject_id=str(row["node_id"]),
            subject_label=str(row["title"] or "Someone"),
            evidence_refs=[f"graph:birthday:{row['node_id']}"],
            meta={"horizon_days": int(horizon), "relationship_tier": tier},
        )
    return None


def _relationship_tier(conn, subject_id: str) -> int:
    row = conn.execute(
        "SELECT source_refs_json FROM graph_edges WHERE from_node_id=? AND rel_kind='knows' LIMIT 1",
        (subject_id,),
    ).fetchone()
    if row:
        meta = json.loads(row["source_refs_json"] or "{}")
        return int(meta.get("tier", 2))
    return 1


def _register_reference_patterns() -> None:
    register_pattern(
        PatternSpec(
            pattern_id="contact_drift",
            domain="relationships",
            event_type="contact_drift",
            proposal_spec=ProposalSpec(
                proposal_type="outbound_message",
                skill_id="send_message",
                intensity="standard",
                leverage_weight=1.0,
            ),
            detect=detect_contact_drift,
            enabled=True,
        )
    )
    register_pattern(
        PatternSpec(
            pattern_id="date_horizon",
            domain="relationships",
            event_type="date_horizon",
            proposal_spec=ProposalSpec(
                proposal_type="commerce_action",
                skill_id="execute_purchase",
                intensity="elevated",
                leverage_weight=2.0,
            ),
            detect=detect_date_horizon,
            enabled=True,
        )
    )
    register_pattern(
        PatternSpec(
            pattern_id="first_meeting_elapsed",
            domain="relationships",
            event_type="first_meeting_elapsed",
            proposal_spec=ProposalSpec(
                proposal_type="calendar_hold",
                skill_id="create_calendar_hold",
                intensity="standard",
                leverage_weight=0.5,
            ),
            detect=lambda _c, _d, **_: None,
            enabled=False,
        )
    )


_register_reference_patterns()
