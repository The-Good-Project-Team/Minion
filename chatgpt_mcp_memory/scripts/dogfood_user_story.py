#!/usr/bin/env python3
"""Dogfood the local Minion sidecar against the user's real data dir.

This is intentionally mutating: it appends one sentinel screen event to the live
ambient stream, then verifies the running sidecar can remember it and explain
what to do next. The event is marked `dogfood:*` so it is auditable.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASES = ("http://127.0.0.1:8765", "http://127.0.0.1:9999", "http://127.0.0.1:9876")


def request(method: str, url: str, token: str = "", body: dict[str, Any] | None = None) -> tuple[int, Any]:
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if raw is not None:
        headers["Content-Type"] = "application/json"
    if token and method not in ("GET", "HEAD", "OPTIONS"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text) if text else {}
        except json.JSONDecodeError:
            return e.code, text
    except Exception as e:
        return 0, {"error": str(e)}


def choose_sidecar() -> tuple[str, dict[str, Any]]:
    explicit = os.environ.get("MINION_LIVE_API_URL", "").strip().rstrip("/")
    bases = [explicit] if explicit else []
    bases.extend(DEFAULT_BASES)
    for base in dict.fromkeys(b for b in bases if b):
        code, body = request("GET", f"{base}/status")
        if code == 200 and isinstance(body, dict):
            return base, body
    raise SystemExit("FAIL no running Minion sidecar found on 8765/9999/9876")


def token_for(data_dir: Path) -> str:
    env = os.environ.get("MINION_API_TOKEN", "").strip()
    if env:
        return env
    p = data_dir / ".minion_api_token"
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def append_dogfood_event(data_dir: Path) -> str:
    ts = time.time()
    marker = f"dogfood-user-story-{int(ts)}"
    stream = data_dir / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": ts,
        "kind": "window_snapshot",
        "app_name": "Cursor",
        "window_title": "Dogfood Minion user-story test",
        "window_id": marker,
        "ax_hash": marker,
        "ax_text_sample": (
            "DOGFOOD_EXPECTATION: user worked on Minion dogfood acceptance test. "
            "Minion should remember this work and suggest a next action."
        ),
        "dedupe_key": marker,
    }
    with stream.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return marker


def main() -> int:
    base, status = choose_sidecar()
    data_dir = Path(str(status.get("data_dir") or "")).expanduser()
    if not data_dir:
        raise SystemExit("FAIL sidecar /status did not expose data_dir")
    token = token_for(data_dir)
    marker = append_dogfood_event(data_dir)
    print(f"INFO sidecar={base}")
    print(f"INFO data_dir={data_dir}")
    print(f"INFO marker={marker}")

    code, remembered = request(
        "POST",
        f"{base}/screen-memory/remember",
        token,
        {"ingest_screenshots": False, "run_adapters": False, "max_lines": 2000},
    )
    if code < 200 or code >= 300:
        print(f"FAIL remember returned {code}: {remembered}")
        return 1
    ambient = int(((remembered or {}).get("ambient") or {}).get("ingested") or 0)
    fused = int(((remembered or {}).get("fused_events") or {}).get("upserted") or 0)
    indexed = int(((remembered or {}).get("event_index") or {}).get("indexed") or 0)
    print(f"PASS remember_screen ambient={ambient} fused={fused} indexed={indexed}")
    if ambient < 1:
        print("FAIL dogfood event was not ingested")
        return 1

    code, recall = request("GET", f"{base}/screen-memory/what-was-i-doing?minutes=30")
    if code != 200:
        print(f"FAIL what-was-i-doing returned {code}: {recall}")
        return 1
    recall_text = json.dumps(recall, ensure_ascii=False)
    if "DOGFOOD_EXPECTATION" not in recall_text and "Dogfood Minion user-story test" not in recall_text:
        print(f"FAIL recall did not include dogfood work: {recall_text[:800]}")
        return 1
    print("PASS recall includes dogfood work context")

    code, guidance = request("GET", f"{base}/screen-memory/guidance?minutes=30")
    if code != 200:
        print(f"FAIL guidance returned {code}: {guidance}")
        return 1
    if not str((guidance or {}).get("do_this") or "").strip():
        print(f"FAIL guidance missing next action: {guidance}")
        return 1
    print(f"PASS guidance mode={guidance.get('mode')} do_this={guidance.get('do_this')}")

    query = urllib.parse.quote("DOGFOOD_EXPECTATION Minion dogfood acceptance test")
    code, search = request("GET", f"{base}/screen-memory/search?q={query}&top_k=5")
    if code != 200:
        print(f"FAIL screen search returned {code}: {search}")
        return 1
    hits = (search or {}).get("hits") or []
    if not hits:
        print(f"FAIL screen search returned no hits: {search}")
        return 1
    print(f"PASS screen search hits={len(hits)} top_kind={hits[0].get('kind')}")
    print("PASS dogfood user story")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
