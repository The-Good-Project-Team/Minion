"""User-togglable runtime settings (persisted to <data_dir>/settings.json)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List

from parsers import ALL_KINDS, set_disabled_kinds


log = logging.getLogger("minion.settings")

SETTINGS_FILENAME = "settings.json"

DEFAULT_AMBIENT_COLLECTORS: Dict[str, bool] = {
    "window_focus": True,
    "ax_content_changed": True,
    "process_snapshot": False,
    "app_launched": True,
    "browser_visit": True,
    "listening": True,
    "full_listening": False,
    "screenshot_fallback": True,
    "screen_reader": True,
}

DEFAULT_AMBIENT_DENY: Dict[str, List[str]] = {
    "app_names": ["1Password", "Keychain Access"],
    "title_substrings": ["password"],
}


def _settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / SETTINGS_FILENAME


def load_settings(data_dir: Path) -> Dict[str, Any]:
    p = _settings_path(data_dir)
    if not p.exists():
        return _default()
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        log.exception("settings: failed to read %s; using defaults", p)
        return _default()
    if not isinstance(data, dict):
        return _default()
    return _normalize(data)


def save_settings(data_dir: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    p = _settings_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize(data)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return normalized


def apply_settings(data: Dict[str, Any]) -> None:
    """Wire settings into the runtime (parser registry, etc.)."""
    disabled = data.get("disabled_kinds") or []
    set_disabled_kinds(disabled)


def full_listening_enabled(data: Dict[str, Any]) -> bool:
    if bool(data.get("full_listening_enabled")):
        return True
    collectors = data.get("ambient_collectors") or {}
    if isinstance(collectors, dict):
        return bool(collectors.get("full_listening"))
    return False


def capture_on_empty_ax_enabled(data: Dict[str, Any]) -> bool:
    if "capture_on_empty_ax" in data:
        return bool(data.get("capture_on_empty_ax"))
    collectors = data.get("ambient_collectors") or {}
    if isinstance(collectors, dict) and collectors.get("screen_reader") is False:
        return False
    return True


def ambient_deny_lists(data: Dict[str, Any]) -> Dict[str, List[str]]:
    raw = data.get("ambient_deny") or {}
    if not isinstance(raw, dict):
        return dict(DEFAULT_AMBIENT_DENY)
    apps = [str(x).strip() for x in (raw.get("app_names") or []) if str(x).strip()]
    subs = [str(x).strip() for x in (raw.get("title_substrings") or []) if str(x).strip()]
    if not apps:
        apps = list(DEFAULT_AMBIENT_DENY["app_names"])
    if not subs:
        subs = list(DEFAULT_AMBIENT_DENY["title_substrings"])
    return {"app_names": apps, "title_substrings": subs}


def ambient_capture_denied(data: Dict[str, Any], app_name: str, title: str) -> bool:
    deny = ambient_deny_lists(data)
    app_l = (app_name or "").strip().casefold()
    title_l = (title or "").strip().casefold()
    for a in deny["app_names"]:
        if app_l == a.strip().casefold():
            return True
    for sub in deny["title_substrings"]:
        if sub.strip().casefold() and sub.strip().casefold() in title_l:
            return True
    return False


def ambient_collector_enabled(data: Dict[str, Any], key: str) -> bool:
    if not data.get("ambient_sensing_enabled", True):
        return False
    collectors = data.get("ambient_collectors") or {}
    if not isinstance(collectors, dict):
        return DEFAULT_AMBIENT_COLLECTORS.get(key, True)
    return bool(collectors.get(key, DEFAULT_AMBIENT_COLLECTORS.get(key, True)))


def _default() -> Dict[str, Any]:
    return {
        "disabled_kinds": [],
        "telemetry_opt_out": False,
        "ambient_sensing_enabled": True,
        "full_listening_enabled": False,
        "capture_on_empty_ax": True,
        "ambient_deny": dict(DEFAULT_AMBIENT_DENY),
        "ambient_collectors": dict(DEFAULT_AMBIENT_COLLECTORS),
    }


def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    raw = out.get("disabled_kinds") or []
    if isinstance(raw, str):
        raw = [raw]
    cleaned: List[str] = []
    seen: set[str] = set()
    for k in raw:
        if not isinstance(k, str):
            continue
        k = k.strip()
        if k in ALL_KINDS and k not in seen:
            cleaned.append(k)
            seen.add(k)
    out["disabled_kinds"] = cleaned
    out.pop("analytics_opt_in", None)
    tot = out.get("telemetry_opt_out")
    out["telemetry_opt_out"] = bool(tot) if tot is not None else False
    out["ambient_sensing_enabled"] = bool(out.get("ambient_sensing_enabled", True))
    raw_ac = out.get("ambient_collectors") or {}
    merged = dict(DEFAULT_AMBIENT_COLLECTORS)
    if isinstance(raw_ac, dict):
        for k, v in raw_ac.items():
            if k in merged:
                merged[k] = bool(v)
            elif k == "full_listening":
                merged["full_listening"] = bool(v)
    fl = bool(out.get("full_listening_enabled")) or merged.get("full_listening", False)
    out["full_listening_enabled"] = fl
    merged["full_listening"] = fl
    out["ambient_collectors"] = merged
    out["capture_on_empty_ax"] = bool(out.get("capture_on_empty_ax", True))
    deny = out.get("ambient_deny") or {}
    if not isinstance(deny, dict):
        deny = {}
    out["ambient_deny"] = ambient_deny_lists({"ambient_deny": deny})
    merged.pop("fs_event", None)
    return out
