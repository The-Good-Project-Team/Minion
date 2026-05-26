#!/usr/bin/env python3
"""Generic VLM adapter wrapper for Minion screen memory.

Set GENERAL_VLM_CMD to a command that accepts one image path and prints JSON or
plain text. This wrapper normalizes the result into Minion's lowest-trust
`general_vlm` contract.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: general_vlm_json_adapter.py <screenshot-path>", file=sys.stderr)
        return 2
    path = Path(argv[1]).expanduser().resolve()
    if not path.is_file():
        print(f"missing screenshot input: {path}", file=sys.stderr)
        return 2
    cmd = os.environ.get("GENERAL_VLM_CMD", "").strip()
    if not cmd:
        print("set GENERAL_VLM_CMD to your visual caption command", file=sys.stderr)
        return 2
    try:
        parsed = run_command(cmd, path)
    except Exception as exc:
        print(f"general VLM adapter failed: {exc}", file=sys.stderr)
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
        timeout=int(os.environ.get("GENERAL_VLM_TIMEOUT_SEC", "60")),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:500])
    body = proc.stdout.strip()
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"scene": body}


def normalize_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, str):
        result = {"scene": result}
    if not isinstance(result, dict):
        result = {"scene": str(result)}
    return {
        "scene": _first_text(result, "scene", "summary", "caption", "description", "text"),
        "confidence": _float(result.get("confidence")) or 0.35,
    }


def _first_text(src: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = src.get(key)
        if val:
            return str(val).strip()[:1000]
    return ""


def _float(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
