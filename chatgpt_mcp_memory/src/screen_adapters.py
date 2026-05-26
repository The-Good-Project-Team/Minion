"""External screen-understanding adapters.

Minion owns the local capture stream. Heavy visual models stay outside this
repo and feed back normalized ambient records when explicitly configured.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def run_external_screen_adapters(
    data_dir: Path,
    *,
    max_items: int = 8,
    timeout_sec: int = 45,
) -> Dict[str, Any]:
    """Run configured sidecar adapters and append normalized ambient records."""
    data = Path(data_dir).expanduser().resolve()
    ambient_dir = data / "ambient"
    inbox_screen = data.parent / "inbox" / "screen-memory"
    out = {
        "playwright_dom": _run_url_adapter(
            name="playwright_dom",
            env_key="MINION_PLAYWRIGHT_DOM_CMD",
            kind="dom_snapshot",
            data_dir=data,
            urls=_latest_browser_urls(data, max_items),
            timeout_sec=timeout_sec,
        ),
        "marlin": _run_adapter(
            name="marlin",
            env_key="MINION_MARLIN_CMD",
            kind="marlin_event",
            data_dir=data,
            candidates=_latest_files([ambient_dir / "video", inbox_screen], VIDEO_EXTS, max_items),
            timeout_sec=timeout_sec,
        ),
        "omniparser": _run_adapter(
            name="omniparser",
            env_key="MINION_OMNIPARSER_CMD",
            kind="omniparser_parse",
            data_dir=data,
            candidates=_latest_files([inbox_screen, ambient_dir / "screenshots"], IMAGE_EXTS, max_items),
            timeout_sec=timeout_sec,
        ),
        "general_vlm": _run_adapter(
            name="general_vlm",
            env_key="MINION_GENERAL_VLM_CMD",
            kind="general_vlm",
            data_dir=data,
            candidates=_latest_files([inbox_screen, ambient_dir / "screenshots"], IMAGE_EXTS, max_items),
            timeout_sec=timeout_sec,
        ),
    }
    out["appended"] = (
        int(out["playwright_dom"].get("appended", 0))
        + int(out["marlin"].get("appended", 0))
        + int(out["omniparser"].get("appended", 0))
        + int(out["general_vlm"].get("appended", 0))
    )
    return out


def screen_adapter_status(data_dir: Path, *, max_items: int = 8) -> Dict[str, Any]:
    """Inspect configured external adapters without executing them."""
    data = Path(data_dir).expanduser().resolve()
    ambient_dir = data / "ambient"
    inbox_screen = data.parent / "inbox" / "screen-memory"
    return {
        "playwright_dom": _adapter_config_status(
            env_key="MINION_PLAYWRIGHT_DOM_CMD",
            candidates=_latest_browser_urls(data, max_items),
            default_command=_default_playwright_dom_cmd(),
            setup_hint="Built in. Override with MINION_PLAYWRIGHT_DOM_CMD or disable with MINION_DISABLE_PLAYWRIGHT_DOM=1.",
        ),
        "marlin": _adapter_config_status(
            env_key="MINION_MARLIN_CMD",
            candidates=_latest_files([ambient_dir / "video", inbox_screen], VIDEO_EXTS, max_items),
            setup_hint="Set MINION_MARLIN_CMD to a Marlin-2B wrapper command; see docs/SCREEN_ADAPTERS.md.",
        ),
        "omniparser": _adapter_config_status(
            env_key="MINION_OMNIPARSER_CMD",
            candidates=_latest_files([inbox_screen, ambient_dir / "screenshots"], IMAGE_EXTS, max_items),
            setup_hint="Set MINION_OMNIPARSER_CMD to an OmniParser wrapper command; see docs/SCREEN_ADAPTERS.md.",
        ),
        "general_vlm": _adapter_config_status(
            env_key="MINION_GENERAL_VLM_CMD",
            candidates=_latest_files([inbox_screen, ambient_dir / "screenshots"], IMAGE_EXTS, max_items),
            setup_hint="Optional lowest-trust fallback. Set MINION_GENERAL_VLM_CMD to a visual caption command; see docs/SCREEN_ADAPTERS.md.",
        ),
    }


def probe_screen_adapters(
    data_dir: Path,
    *,
    timeout_sec: int = 30,
) -> Dict[str, Any]:
    """Run configured heavy adapters against one recent input without appending records."""
    data = Path(data_dir).expanduser().resolve()
    ambient_dir = data / "ambient"
    inbox_screen = data.parent / "inbox" / "screen-memory"
    return {
        "marlin": _probe_file_adapter(
            env_key="MINION_MARLIN_CMD",
            kind="marlin_event",
            data_dir=data,
            candidates=_latest_files([ambient_dir / "video", inbox_screen], VIDEO_EXTS, 1),
            timeout_sec=timeout_sec,
        ),
        "omniparser": _probe_file_adapter(
            env_key="MINION_OMNIPARSER_CMD",
            kind="omniparser_parse",
            data_dir=data,
            candidates=_latest_files([inbox_screen, ambient_dir / "screenshots"], IMAGE_EXTS, 1),
            timeout_sec=timeout_sec,
        ),
        "general_vlm": _probe_file_adapter(
            env_key="MINION_GENERAL_VLM_CMD",
            kind="general_vlm",
            data_dir=data,
            candidates=_latest_files([inbox_screen, ambient_dir / "screenshots"], IMAGE_EXTS, 1),
            timeout_sec=timeout_sec,
        ),
    }


def _adapter_config_status(
    *,
    env_key: str,
    candidates: List[Any],
    default_command: str = "",
    setup_hint: str = "",
) -> Dict[str, Any]:
    template = _adapter_command(env_key, default_command=default_command)
    return {
        "configured": bool(template),
        "env_key": env_key,
        "command_preview": template[:240],
        "candidates": len(candidates),
        "latest_inputs": [str(p) for p in candidates[:5]],
        "setup_hint": setup_hint,
    }


def _probe_file_adapter(
    *,
    env_key: str,
    kind: str,
    data_dir: Path,
    candidates: List[Path],
    timeout_sec: int,
) -> Dict[str, Any]:
    template = _adapter_command(env_key)
    if not template:
        return {"configured": False, "ok": False, "reason": "not_configured", "candidates": len(candidates)}
    if not candidates:
        return {"configured": True, "ok": False, "reason": "no_recent_input", "candidates": 0}
    path = candidates[0]
    try:
        raw_records = _run_command(template, path, data_dir=data_dir, timeout_sec=timeout_sec)
        records = [
            r for r in (_normalize_adapter_record(kind=kind, raw=raw, path=path) for raw in raw_records)
            if r
        ]
        return {
            "configured": True,
            "ok": bool(records),
            "input": str(path),
            "records": len(records),
            "sample": records[0] if records else None,
        }
    except Exception as exc:
        return {
            "configured": True,
            "ok": False,
            "input": str(path),
            "reason": "command_failed",
            "error": str(exc)[:500],
        }


def _run_adapter(
    *,
    name: str,
    env_key: str,
    kind: str,
    data_dir: Path,
    candidates: List[Path],
    timeout_sec: int,
) -> Dict[str, Any]:
    template = _adapter_command(env_key)
    if not template:
        return {"configured": False, "appended": 0, "candidates": len(candidates)}
    appended = 0
    errors: List[Dict[str, str]] = []
    records: List[Dict[str, Any]] = []
    for path in candidates:
        try:
            raw_records = _run_command(template, path, data_dir=data_dir, timeout_sec=timeout_sec)
            for raw in raw_records:
                record = _normalize_adapter_record(kind=kind, raw=raw, path=path)
                if not record:
                    continue
                _append_ambient_record(data_dir, record)
                appended += 1
                records.append(record)
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)[:500]})
    return {
        "configured": True,
        "adapter": name,
        "appended": appended,
        "candidates": len(candidates),
        "errors": errors[:5],
        "records": records[:5],
    }


def _run_url_adapter(
    *,
    name: str,
    env_key: str,
    kind: str,
    data_dir: Path,
    urls: List[str],
    timeout_sec: int,
) -> Dict[str, Any]:
    template = _adapter_command(env_key, default_command=_default_playwright_dom_cmd())
    if not template:
        return {"configured": False, "appended": 0, "candidates": len(urls)}
    appended = 0
    errors: List[Dict[str, str]] = []
    records: List[Dict[str, Any]] = []
    for url in urls:
        try:
            raw_records = _run_command(template, url, data_dir=data_dir, timeout_sec=timeout_sec)
            for raw in raw_records:
                record = _normalize_url_adapter_record(kind=kind, raw=raw, url=url)
                if not record:
                    continue
                _append_ambient_record(data_dir, record)
                appended += 1
                records.append(record)
        except Exception as exc:
            errors.append({"url": url, "error": str(exc)[:500]})
    return {
        "configured": True,
        "adapter": name,
        "appended": appended,
        "candidates": len(urls),
        "errors": errors[:5],
        "records": records[:5],
    }


def _run_command(template: str, path: Path, *, data_dir: Path, timeout_sec: int) -> List[Dict[str, Any]]:
    rendered = template.format(input=str(path), url=str(path), data_dir=str(data_dir))
    args = shlex.split(rendered)
    if "{input}" not in template and "{url}" not in template:
        args.append(str(path))
    if not args:
        return []
    proc = subprocess.run(
        args,
        cwd=str(data_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=max(1, timeout_sec),
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"adapter exited {proc.returncode}: {msg[:400]}")
    return _parse_json_records(proc.stdout)


def _adapter_command(env_key: str, *, default_command: str = "") -> str:
    template = os.environ.get(env_key, "").strip()
    if template:
        return template
    if env_key == "MINION_PLAYWRIGHT_DOM_CMD":
        disabled = os.environ.get("MINION_DISABLE_PLAYWRIGHT_DOM", "").strip().lower()
        if disabled not in ("1", "true", "on") and default_command:
            return default_command
    return ""


def _parse_json_records(text: str) -> List[Dict[str, Any]]:
    body = text.strip()
    if not body:
        return []
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        out: List[Dict[str, Any]] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    return []


def _normalize_adapter_record(*, kind: str, raw: Dict[str, Any], path: Path) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    record = dict(raw)
    record["kind"] = kind
    record.setdefault("ts", time.time())
    record.setdefault("source_path", str(path))
    record.setdefault("source_sha1", _file_fingerprint(path))
    record.setdefault("dedupe_key", f"{kind}:{record['source_sha1']}")
    if kind == "marlin_event":
        record.setdefault("scene", record.get("summary") or record.get("caption") or "")
        record.setdefault("confidence", 0.78)
        _normalize_temporal_fields(record)
    elif kind == "omniparser_parse":
        if "elements" in record and "visible_elements" not in record:
            record["visible_elements"] = record.get("elements")
        record.setdefault("confidence", 0.74)
    elif kind == "general_vlm":
        record.setdefault(
            "scene",
            record.get("summary") or record.get("caption") or record.get("description") or record.get("text") or "",
        )
        record.setdefault("confidence", 0.35)
    return record


def _normalize_url_adapter_record(*, kind: str, raw: Dict[str, Any], url: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    record = dict(raw)
    record["kind"] = kind
    record.setdefault("ts", time.time())
    record.setdefault("url", url)
    record.setdefault("source_url", url)
    record.setdefault("source", "playwright_dom")
    record.setdefault("dedupe_key", f"{kind}:playwright:{_sha1_text(url + json.dumps(raw, sort_keys=True))}")
    if kind == "dom_snapshot":
        if "elements" in record and "visible_elements" not in record:
            record["visible_elements"] = record.get("elements")
        record.setdefault("confidence", 0.96)
    return record


def _normalize_temporal_fields(record: Dict[str, Any]) -> None:
    start = _first_number(record, "start_sec", "start_seconds", "start_time", "start", "timestamp_sec", "timestamp")
    end = _first_number(record, "end_sec", "end_seconds", "end_time", "end")
    duration = _first_number(record, "duration_sec", "duration")
    if end is None and start is not None and duration is not None:
        end = start + duration
    if start is not None:
        record["start_sec"] = start
    if end is not None:
        record["end_sec"] = end
    if start is not None or end is not None:
        record["time_range"] = _format_time_range(start, end)
    events = record.get("events")
    if isinstance(events, list):
        for item in events:
            if isinstance(item, dict):
                _normalize_temporal_fields(item)


def _first_number(record: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key not in record:
            continue
        try:
            return float(record[key])
        except (TypeError, ValueError):
            continue
    return None


def _format_time_range(start: Optional[float], end: Optional[float]) -> str:
    if start is not None and end is not None:
        return f"{start:g}s-{end:g}s"
    if start is not None:
        return f"{start:g}s"
    return f"-{end:g}s" if end is not None else ""


def _latest_files(roots: Iterable[Path], exts: set[str], limit: int) -> List[Path]:
    files: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in exts:
                files.append(path)
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    return files[: max(0, limit)]


def _latest_browser_urls(data_dir: Path, limit: int) -> List[str]:
    path = Path(data_dir).expanduser().resolve() / "ambient" / "stream.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for line in reversed(lines[-1000:]):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("kind") or "") not in ("browser_visit", "dom_snapshot", "window_snapshot"):
            continue
        url = str(row.get("url") or row.get("url_or_host") or "").strip()
        if not url.startswith(("http://", "https://", "data:")) or url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= max(0, limit):
            break
    return out


def _default_playwright_dom_cmd() -> str:
    script = Path(__file__).resolve().parents[2] / "desktop" / "scripts" / "playwright-dom-snapshot.mjs"
    if not script.is_file():
        return ""
    return f"node {shlex.quote(str(script))} {{url}}"


def _file_fingerprint(path: Path) -> str:
    try:
        st = path.stat()
        seed = f"{path.resolve()}:{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        seed = str(path)
    return hashlib.sha1(seed.encode("utf-8", errors="replace")).hexdigest()


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _append_ambient_record(data_dir: Path, record: Dict[str, Any]) -> None:
    path = data_dir / "ambient" / "stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
