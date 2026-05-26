#!/usr/bin/env python3
"""Marlin-2B adapter wrapper for Minion screen memory.

This script intentionally lives outside the package runtime path. It is a
thin bridge around a separately installed Marlin/Hugging Face environment and
prints Minion's normalized JSON adapter contract to stdout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


MODEL_ID = os.environ.get("MINION_MARLIN_MODEL", "NemoStation/Marlin-2B")


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("usage: marlin_hf_adapter.py <video-path>", file=sys.stderr)
        return 2
    path = Path(argv[1]).expanduser().resolve()
    if not path.is_file():
        print(f"missing video input: {path}", file=sys.stderr)
        return 2
    try:
        result = run_marlin(path)
    except Exception as exc:
        print(f"marlin adapter failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(normalize_result(result), ensure_ascii=False))
    return 0


def run_marlin(path: Path) -> Any:
    try:
        import torch  # noqa: F401
        from transformers import AutoModelForCausalLM, AutoProcessor
    except Exception as exc:
        raise RuntimeError(
            "install Marlin dependencies in this environment: torch, transformers, "
            "and the model-specific requirements"
        ) from exc

    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        trust_remote_code=True,
        device_map=os.environ.get("MINION_MARLIN_DEVICE_MAP", "auto"),
    )
    processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)

    if hasattr(model, "caption"):
        return model.caption(str(path))
    if hasattr(model, "generate_caption"):
        return model.generate_caption(str(path))
    if callable(processor):
        inputs = processor(videos=str(path), return_tensors="pt")
        out = model.generate(**inputs)
        text = processor.batch_decode(out, skip_special_tokens=True)[0]
        return {"scene": text}
    raise RuntimeError("loaded Marlin model does not expose caption/generate_caption and processor is not callable")


def normalize_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, str):
        result = {"scene": result}
    if not isinstance(result, dict):
        result = {"scene": str(result)}
    scene = _first_text(result, "scene", "summary", "caption", "description")
    events = [_normalize_event(e) for e in _iter_events(result)]
    out: Dict[str, Any] = {
        "scene": scene,
        "events": [e for e in events if e.get("summary") or e.get("time_range")],
        "confidence": _first_float(result, "confidence") or 0.78,
    }
    _copy_time_fields(result, out)
    return out


def _iter_events(result: Dict[str, Any]) -> Iterable[Any]:
    raw = result.get("events") or result.get("segments") or result.get("captions") or []
    return raw if isinstance(raw, list) else []


def _normalize_event(item: Any) -> Dict[str, Any]:
    if isinstance(item, str):
        return {"type": "scene_event", "summary": item}
    if not isinstance(item, dict):
        return {"type": "scene_event", "summary": str(item)}
    out: Dict[str, Any] = {
        "type": str(item.get("type") or item.get("label") or "scene_event"),
        "summary": _first_text(item, "summary", "caption", "description", "text"),
        "confidence": _first_float(item, "confidence"),
    }
    _copy_time_fields(item, out)
    return {k: v for k, v in out.items() if v not in (None, "")}


def _copy_time_fields(src: Dict[str, Any], dst: Dict[str, Any]) -> None:
    for src_key, dst_key in (
        ("start_sec", "start_sec"),
        ("start_time", "start_sec"),
        ("timestamp", "start_sec"),
        ("timestamp_sec", "start_sec"),
        ("end_sec", "end_sec"),
        ("end_time", "end_sec"),
        ("duration", "duration"),
        ("duration_sec", "duration"),
    ):
        val = _float(src.get(src_key))
        if val is not None and dst_key not in dst:
            dst[dst_key] = val
    if "time_range" in src and src["time_range"]:
        dst["time_range"] = str(src["time_range"])


def _first_text(src: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = src.get(key)
        if val:
            return str(val)
    return ""


def _first_float(src: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        val = _float(src.get(key))
        if val is not None:
            return val
    return None


def _float(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
