"""Refresh Contacts/Calendar JSON under <data_dir>/life_evidence (macOS)."""
from __future__ import annotations

import json
import logging
import platform
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

_EVIDENCE_DIR = "life_evidence"
_CONTACTS_FILE = "contacts_latest.json"


def evidence_dir(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / _EVIDENCE_DIR


def load_contact_names(data_dir: Path) -> List[str]:
    path = evidence_dir(data_dir) / _CONTACTS_FILE
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("contacts", [])
        out: List[str] = []
        seen: set[str] = set()
        for c in items:
            if not isinstance(c, dict):
                continue
            name = (c.get("display_name") or c.get("name") or "").strip()
            key = _norm_name(name)
            if name and key and key not in seen:
                seen.add(key)
                out.append(name)
        return out
    except Exception:
        log.debug("load_contact_names failed", exc_info=True)
        return []


def snapshot_contacts_to_file(data_dir: Path) -> int:
    """macOS Contacts → life_evidence/contacts_latest.json. Returns count."""
    root = evidence_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    contacts = _macos_contacts_list()
    path = root / _CONTACTS_FILE
    path.write_text(
        json.dumps(contacts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(contacts)


def refresh_life_evidence_if_stale(
    data_dir: Path,
    *,
    max_age_sec: float = 6 * 3600,
) -> Dict[str, Any]:
    """Refresh contacts snapshot when missing or older than max_age_sec."""
    path = evidence_dir(data_dir) / _CONTACTS_FILE
    refreshed = False
    if platform.system() == "Darwin":
        stale = not path.is_file()
        if path.is_file():
            stale = (time.time() - path.stat().st_mtime) > max_age_sec
        if stale:
            try:
                snapshot_contacts_to_file(data_dir)
                refreshed = True
            except Exception:
                log.warning("contacts snapshot failed", exc_info=True)
    names = load_contact_names(data_dir)
    return {"contacts": len(names), "refreshed": refreshed}


def _macos_contacts_list() -> List[Dict[str, Any]]:
    if platform.system() != "Darwin":
        return []
    script = r'''
        set out to ""
        tell application "Contacts"
            repeat with p in people
                set nm to ""
                try
                    set nm to name of p
                end try
                if nm is not "" then
                    set out to out & nm & (ASCII character 10)
                end if
            end repeat
        end tell
        return out
    '''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        log.warning("osascript contacts failed", exc_info=True)
        return []
    if proc.returncode != 0:
        log.warning("contacts AppleScript exit %s: %s", proc.returncode, proc.stderr[:200])
        return []
    names = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return [{"display_name": n, "source": "macos_contacts"} for n in names]


def _norm_name(name: str) -> str:
    return " ".join((name or "").lower().split())
