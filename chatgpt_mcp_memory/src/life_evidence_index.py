"""Index life evidence snapshots (calendar/contacts JSON from desktop)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)

_EVIDENCE_DIR = "life_evidence"


def evidence_dir(data_dir) -> Path:
    return Path(data_dir) / _EVIDENCE_DIR


def ingest_life_evidence(data_dir, conn) -> Dict[str, int]:
    """Read latest contacts/calendar JSON dumps and update graph."""
    d = evidence_dir(data_dir)
    if not d.is_dir():
        return {"contacts": 0, "calendar": 0}
    contacts_n = 0
    cal_n = 0
    contacts_path = d / "contacts_latest.json"
    if contacts_path.is_file():
        try:
            raw = json.loads(contacts_path.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("contacts", [])
            from entity_resolution import ingest_contacts_snapshot

            contacts_n = ingest_contacts_snapshot(conn, items)
        except Exception:
            log.exception("contacts evidence ingest failed")
    cal_path = d / "calendar_latest.json"
    if cal_path.is_file():
        try:
            raw = json.loads(cal_path.read_text(encoding="utf-8"))
            events = raw if isinstance(raw, list) else raw.get("events", [])
            cal_n = len(events)
        except Exception:
            log.exception("calendar evidence read failed")
    return {"contacts": contacts_n, "calendar": cal_n}
