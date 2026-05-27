#!/usr/bin/env python3
"""Smoke-test a running Minion sidecar, or start one from this checkout."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = Path.home() / "Library/Application Support/Minion/data"
DEFAULT_INBOX = Path.home() / "Library/Application Support/Minion/inbox"


class Smoke:
    def __init__(self, mutating: bool, start: bool) -> None:
        self.mutating = mutating
        self.start = start
        self.failures: list[str] = []
        self.notes: list[str] = []
        self.api_token = os.environ.get("MINION_API_TOKEN", "").strip()
        self.child: subprocess.Popen[str] | None = None

    def request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> tuple[int, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_token and method not in ("GET", "HEAD", "OPTIONS"):
            headers["Authorization"] = f"Bearer {self.api_token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return e.code, raw
        except Exception as e:
            return 0, {"error": str(e)}

    def ok_status(self, base: str) -> bool:
        code, body = self.request("GET", f"{base}/status", timeout=1.2)
        return code == 200 and isinstance(body, dict)

    def candidate_bases(self) -> list[str]:
        out: list[str] = []
        explicit = os.environ.get("MINION_LIVE_API_URL", "").strip().rstrip("/")
        if explicit:
            out.append(explicit)
        port = os.environ.get("MINION_API_PORT", "").strip()
        if port:
            out.append(f"http://127.0.0.1:{port}")

        try:
            ps = subprocess.check_output(["ps", "-axo", "command="], text=True)
        except Exception:
            ps = ""
        for line in ps.splitlines():
            if "api.py" not in line or "--port" not in line:
                continue
            m = re.search(r"--port(?:=|\s+)(\d+)", line)
            if m:
                out.append(f"http://127.0.0.1:{m.group(1)}")

        out.extend(["http://127.0.0.1:8765", "http://127.0.0.1:9876"])
        return list(dict.fromkeys(out))

    def start_sidecar(self) -> str:
        port = int(os.environ.get("MINION_API_PORT", "8765"))
        python = ROOT / "chatgpt_mcp_memory/.venv/bin/python"
        api = ROOT / "chatgpt_mcp_memory/src/api.py"
        if not python.exists():
            self.failures.append(f"missing Python venv: {python}")
            return ""
        env = os.environ.copy()
        env.setdefault("MINION_DATA_DIR", str(DEFAULT_DATA))
        env.setdefault("MINION_INBOX", str(DEFAULT_INBOX))
        env.setdefault("MINION_DISABLE_WATCHER", "1")
        env.setdefault("MINION_DISABLE_AMBIENT_SCHEDULER", "1")
        env.setdefault("MINION_DISABLE_REMOTE_ANALYTICS", "1")
        env["PYTHONPATH"] = str(ROOT / "chatgpt_mcp_memory/src")
        Path(env["MINION_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
        Path(env["MINION_INBOX"]).mkdir(parents=True, exist_ok=True)
        self.child = subprocess.Popen(
            [str(python), str(api), "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.child.poll() is not None:
                self.failures.append(f"sidecar exited early with code {self.child.returncode}")
                return ""
            if self.ok_status(base):
                print(f"INFO started sidecar on {base}")
                return base
            time.sleep(0.25)
        self.failures.append(f"sidecar did not become ready on {base}")
        return ""

    def choose_base(self) -> str:
        for base in self.candidate_bases():
            if self.ok_status(base):
                return base
        if self.start:
            return self.start_sidecar()
        self.failures.append("no Minion sidecar found; start Minion or pass --start")
        return ""

    def load_token_from_status(self, status: dict[str, Any]) -> None:
        if self.api_token or not self.mutating:
            return
        data_dir = str(status.get("data_dir") or "").strip()
        token_path = Path(data_dir) / ".minion_api_token" if data_dir else None
        if token_path:
            try:
                self.api_token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                self.api_token = ""
        if not self.api_token:
            self.failures.append("mutating mode needs MINION_API_TOKEN or readable <data_dir>/.minion_api_token")

    def check(self, base: str) -> None:
        responses: dict[str, Any] = {}
        checks = [
            ("status", "GET", "/status"),
            ("health", "GET", "/health"),
            ("capabilities", "GET", "/capabilities"),
            ("feed", "GET", "/feed"),
            ("graph_context", "GET", "/graph/context"),
            ("menu_status", "GET", "/menu/status"),
            ("screen_memory_status", "GET", "/screen-memory/status"),
        ]
        for name, method, path in checks:
            code, body = self.request(method, f"{base}{path}")
            if 200 <= code < 300:
                print(f"PASS {name}")
                responses[name] = body
            else:
                self.failures.append(f"{name} {method} {path} returned {code}: {body}")

        status = responses.get("status")
        if isinstance(status, dict):
            self.load_token_from_status(status)
            db = status.get("database")
            if isinstance(db, dict) and db.get("ok") is not True:
                self.failures.append(f"database unhealthy: {db}")
            counts = status.get("counts")
            if isinstance(counts, dict):
                print(f"INFO sources={counts.get('sources', 0)} chunks={counts.get('chunks', 0)}")

        health = responses.get("health")
        if isinstance(health, dict) and health.get("status") not in (None, "ok"):
            self.failures.append(f"health status is {health.get('status')!r}")

        capabilities = responses.get("capabilities")
        if isinstance(capabilities, dict) and capabilities.get("service") != "minion-api":
            self.failures.append(f"capabilities service mismatch: {capabilities.get('service')!r}")

        screen = responses.get("screen_memory_status")
        if isinstance(screen, dict):
            gates = screen.get("completion_gates")
            if isinstance(gates, dict):
                blocked = [g.get("id") for g in gates.get("blocked", []) if isinstance(g, dict)]
                if blocked:
                    print("INFO screen-memory blocked gates: " + ", ".join(str(x) for x in blocked))

        if self.mutating and not self.failures:
            code, body = self.request(
                "POST",
                f"{base}/screen-memory/remember",
                {"ingest_screenshots": False, "run_adapters": False},
                timeout=30.0,
            )
            if 200 <= code < 300 and isinstance(body, dict):
                ambient = (body.get("ambient") or {}).get("ingested", 0)
                fused = (body.get("fused_events") or {}).get("upserted", 0)
                indexed = (body.get("event_index") or {}).get("indexed", 0)
                print(f"PASS remember_screen ambient={ambient} fused={fused} indexed={indexed}")
            else:
                self.failures.append(f"remember_screen returned {code}: {body}")

    def run(self) -> int:
        try:
            base = self.choose_base()
            if base:
                print(f"INFO sidecar={base}")
                self.check(base)
            for note in self.notes:
                print(f"INFO {note}")
            if self.failures:
                for failure in self.failures:
                    print(f"FAIL {failure}")
                return 1
            print("PASS live smoke")
            return 0
        finally:
            if self.child is not None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutating", action="store_true", help="also run live remember-screen")
    parser.add_argument(
        "--start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="start a local sidecar when none is running (default: true)",
    )
    args = parser.parse_args()
    return Smoke(mutating=args.mutating, start=args.start).run()


if __name__ == "__main__":
    sys.exit(main())
