"""Learning loop: user approvals update pattern cadence and suppression."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from council_store import council_pattern_state_upsert

_REJECT_BASE_DAYS = 30.0
_REJECT_MULTIPLIER = 1.5
_DEFAULT_SNOOZE_DAYS = 7.0
_DEFAULT_CADENCE_DAYS = 21.0


def apply_approval_learning(
    conn,
    *,
    pattern_id: str,
    subject_id: str,
    action: str,
    snooze_days: Optional[float] = None,
    proposal_created_at: Optional[float] = None,
) -> None:
    now = time.time()
    action = (action or "").strip().lower()
    if action in ("reject", "dismiss"):
        _apply_reject(conn, pattern_id, subject_id, now=now)
        return
    if action == "snooze":
        days = float(snooze_days if snooze_days is not None else _DEFAULT_SNOOZE_DAYS)
        council_pattern_state_upsert(
            conn,
            pattern_id=pattern_id,
            subject_id=subject_id,
            suppress_until=now + days * 86400.0,
            last_signal_at=now,
        )
        return
    if action in ("approve", "edit"):
        _apply_approve(
            conn,
            pattern_id,
            subject_id,
            now=now,
            proposal_created_at=proposal_created_at,
        )


def _apply_reject(conn, pattern_id: str, subject_id: str, *, now: float) -> None:
    from council_store import council_pattern_state_get

    st = council_pattern_state_get(conn, pattern_id, subject_id)
    rc = int((st or {}).get("reject_count") or 0) + 1
    days = _REJECT_BASE_DAYS * (_REJECT_MULTIPLIER ** max(0, rc - 1))
    council_pattern_state_upsert(
        conn,
        pattern_id=pattern_id,
        subject_id=subject_id,
        reject_count=rc,
        suppress_until=now + days * 86400.0,
        last_signal_at=now,
    )


def _apply_approve(
    conn,
    pattern_id: str,
    subject_id: str,
    *,
    now: float,
    proposal_created_at: Optional[float],
) -> None:
    from council_store import council_pattern_state_get

    st = council_pattern_state_get(conn, pattern_id, subject_id)
    learned = (st or {}).get("learned_cadence_days")
    if learned is None:
        learned = _DEFAULT_CADENCE_DAYS
    if proposal_created_at is not None and st and st.get("last_approved_at"):
        gap_days = (now - float(st["last_approved_at"])) / 86400.0
        if gap_days > 1.0:
            learned = max(7.0, min(90.0, (float(learned) + gap_days) / 2.0))
    council_pattern_state_upsert(
        conn,
        pattern_id=pattern_id,
        subject_id=subject_id,
        reject_count=0,
        suppress_until=None,
        last_approved_at=now,
        last_signal_at=now,
        learned_cadence_days=learned,
    )
