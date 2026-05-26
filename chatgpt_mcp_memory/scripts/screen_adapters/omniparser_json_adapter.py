#!/usr/bin/env python3
"""OmniParser adapter wrapper for Minion screen memory.

Set OMNIPARSER_CMD to a command that accepts one image path and prints JSON.
This wrapper normalizes common OmniParser-like outputs into Minion's
`visible_elements` contract.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("usage: omniparser_json_adapter.py <screenshot-path>", file=sys.stderr)
        return 2
    path = Path(argv[1]).expanduser().resolve()
    if not path.is_file():
        print(f"missing screenshot input: {path}", file=sys.stderr)
        return 2
    cmd = os.environ.get("OMNIPARSER_CMD", "").strip()
    if not cmd:
        print("set OMNIPARSER_CMD to your OmniParser JSON command", file=sys.stderr)
        return 2
    try:
        parsed = run_command(cmd, path)
    except Exception as exc:
        print(f"omniparser adapter failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(normalize_result(parsed), ensure_ascii=False))
    return 0


def run_command(template: str, path: Path) -> Any:
    rendered = template.format(input=str(path))
    args = shlex.split(rendered)
    if "{input}" not in template:
        args.append(str(path))
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=int(os.environ.get("OMNIPARSER_TIMEOUT_SEC", "60")),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:500])
    body = proc.stdout.strip()
    if not body:
        return {}
    return json.loads(body)


def normalize_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, list):
        result = {"elements": result}
    if not isinstance(result, dict):
        result = {}
    raw_elements = (
        result.get("visible_elements")
        or result.get("elements")
        or result.get("parsed_content_list")
        or result.get("detections")
        or []
    )
    elements = [_normalize_element(e) for e in raw_elements if isinstance(e, dict)]
    return {
        "visible_elements": [e for e in elements if e.get("label") or e.get("role")],
        "confidence": _float(result.get("confidence")) or 0.74,
    }


def _normalize_element(item: Dict[str, Any]) -> Dict[str, Any]:
    label = (
        item.get("label")
        or item.get("text")
        or item.get("content")
        or item.get("description")
        or item.get("caption")
        or ""
    )
    role = item.get("role") or item.get("type") or item.get("category") or "element"
    return {
        "role": str(role)[:80],
        "label": str(label).strip()[:300],
        "bounds": item.get("bounds") or item.get("bbox") or item.get("box"),
        "source": "OmniParser",
        "confidence": _float(item.get("confidence") or item.get("score")) or 0.74,
    }


def _float(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
