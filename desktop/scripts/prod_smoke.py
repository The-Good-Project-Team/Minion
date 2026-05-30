#!/usr/bin/env python3
"""Production smoke: build/install/launch Minion.app and drive the app harness."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
DEFAULT_DATA = Path.home() / "Library/Application Support/Minion/data"
DEFAULT_INSTALL = Path.home() / "Applications/Minion-smoke.app"
HARNESS_FILE = "smoke-harness.json"


def native_tauri_target() -> str:
    if sys.platform != "darwin":
        return ""
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "aarch64-apple-darwin"
    if machine in {"x86_64", "amd64"}:
        return "x86_64-apple-darwin"
    return ""


def app_main_binary(app_path: Path) -> Path | None:
    macos_dir = app_path / "Contents" / "MacOS"
    if not macos_dir.is_dir():
        return None
    for candidate in sorted(macos_dir.iterdir()):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


class ProdSmoke:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.data_dir = Path(args.data_dir).expanduser().resolve()
        self.install_path = Path(args.install_path).expanduser().resolve()
        self.failures: list[str] = []
        self.notes: list[str] = []
        self.token = os.environ.get("MINION_API_TOKEN", "").strip()
        self.harness: dict[str, Any] = {}
        self.report: dict[str, Any] = {}

    def request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        *,
        token: str = "",
        timeout: float = 30.0,
    ) -> tuple[int, Any]:
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        auth = token or self.token
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        req = urllib.request.Request(url, data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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

    def load_token(self) -> None:
        if self.token:
            return
        p = self.data_dir / ".minion_api_token"
        if p.is_file():
            self.token = p.read_text(encoding="utf-8").strip()
        if not self.token and self.args.mutating:
            self.failures.append("mutating mode needs MINION_API_TOKEN or readable .minion_api_token")

    def set_launch_env(self) -> None:
        if sys.platform != "darwin":
            self.notes.append("launchctl setenv skipped (non-macOS)")
            return
        env_pairs = {
            "MINION_DATA_DIR": str(self.data_dir),
            "MINION_DISABLE_REMOTE_ANALYTICS": "1",
        }
        if self.args.inbox:
            env_pairs["MINION_INBOX"] = str(Path(self.args.inbox).expanduser().resolve())
        for key, value in env_pairs.items():
            subprocess.run(["launchctl", "setenv", key, value], check=False)
        self.notes.append(f"launchctl set MINION_DATA_DIR={self.data_dir}")

    def find_built_app(self) -> Path | None:
        candidates: list[Path] = []
        target = (self.args.target or "").strip()
        if target:
            candidates.append(DESKTOP / f"src-tauri/target/{target}/release/bundle/macos/Minion.app")
        candidates.extend(
            [
                DESKTOP / "src-tauri/target/release/bundle/macos/Minion.app",
                DESKTOP / "src-tauri/target/aarch64-apple-darwin/release/bundle/macos/Minion.app",
                DESKTOP / "src-tauri/target/x86_64-apple-darwin/release/bundle/macos/Minion.app",
            ]
        )
        globbed = sorted(DESKTOP.glob("src-tauri/target/**/release/bundle/macos/Minion.app"))
        seen: set[Path] = set()
        for p in candidates + globbed:
            if p in seen or not p.is_dir():
                continue
            seen.add(p)
            if self._app_arch_ok(p):
                return p
        return None

    def _app_arch_ok(self, app_path: Path) -> bool:
        binary = app_main_binary(app_path)
        if binary is None:
            return True
        proc = subprocess.run(["file", str(binary)], capture_output=True, text=True)
        desc = (proc.stdout or proc.stderr or "").lower()
        host = platform.machine().lower()
        if host in {"arm64", "aarch64"} and "x86_64" in desc and "arm64" not in desc:
            self.notes.append(f"skipping x86_64 build at {app_path} (host is arm64)")
            return False
        if host in {"x86_64", "amd64"} and "arm64" in desc and "x86_64" not in desc:
            self.notes.append(f"skipping arm64 build at {app_path} (host is x86_64)")
            return False
        return True

    def build_app(self) -> Path | None:
        if self.args.no_build:
            app = self.find_built_app()
            if app:
                self.notes.append(f"using existing build {app}")
            else:
                self.failures.append("no built Minion.app found; run without --no-build")
            return app
        print("INFO building Minion.app (this can take several minutes)")
        target = (self.args.target or "").strip() or native_tauri_target()
        if target:
            print(f"INFO tauri target={target}")
        cmd = ["npm", "run", "tauri", "build"]
        if target:
            cmd.extend(["--", "--target", target])
        result = subprocess.run(cmd, cwd=DESKTOP)
        app = self.find_built_app()
        if result.returncode != 0 and app:
            self.notes.append(
                "tauri build exited nonzero after producing Minion.app; continuing "
                "(usually missing TAURI_SIGNING_PRIVATE_KEY for updater artifacts)"
            )
            return app
        if result.returncode != 0:
            self.failures.append("tauri build failed")
            return None
        if not app:
            self.failures.append("build finished but Minion.app not found")
        return app

    def install_app(self, built: Path) -> Path:
        if self.args.no_install:
            self.notes.append(f"using built app in place {built}")
            return built
        if self.install_path.exists():
            shutil.rmtree(self.install_path)
        shutil.copytree(built, self.install_path)
        self.notes.append(f"installed {self.install_path}")
        self.prepare_local_app(self.install_path)
        return self.install_path

    def prepare_local_app(self, app_path: Path) -> None:
        if sys.platform != "darwin":
            return
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app_path)], check=False)
        proc = subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(app_path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            self.notes.append(f"ad-hoc signed {app_path}")
            return
        msg = (proc.stderr or proc.stdout or "").strip()
        self.notes.append(f"ad-hoc signing failed for {app_path}: {msg or proc.returncode}")

    def launch_app(self, app_path: Path) -> None:
        if self.args.no_launch:
            self.notes.append("skipped app launch (--no-launch)")
            return
        if not self._app_arch_ok(app_path):
            self.failures.append(
                f"cannot launch {app_path}: CPU architecture mismatch; rebuild with "
                f"--target {native_tauri_target() or 'aarch64-apple-darwin'}"
            )
            return
        if not self.args.no_install:
            self.prepare_local_app(app_path)
        proc = subprocess.run(["open", "-n", str(app_path)], capture_output=True, text=True)
        err = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode != 0 or "cannot be opened" in err.lower():
            self.failures.append(f"failed to launch {app_path}: {err or 'open failed'}")
            return
        self.notes.append(f"launched {app_path}")

    def wait_for_harness(self, timeout_sec: float) -> bool:
        path = self.data_dir / HARNESS_FILE
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if path.is_file():
                try:
                    self.harness = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    time.sleep(0.5)
                    continue
                port = int(self.harness.get("port") or 0)
                if port > 0:
                    code, body = self.request("GET", f"http://127.0.0.1:{port}/health", timeout=2.0)
                    if code == 200:
                        return True
            time.sleep(1.0)
        self.failures.append(f"harness not ready within {timeout_sec}s ({path})")
        return False

    def harness_base(self) -> str:
        port = int(self.harness.get("port") or 0)
        host = str(self.harness.get("host") or "127.0.0.1")
        return f"http://{host}:{port}"

    def run_harness_probe(self) -> dict[str, Any]:
        base = self.harness_base()
        code, info = self.request("GET", f"{base}/info", token=self.token, timeout=10.0)
        if code != 200:
            self.failures.append(f"GET /info returned {code}: {info}")
            return {"info": info}
        print(f"INFO sidecar_ready={info.get('sidecar_ready')} api_port={info.get('api_port')}")

        code, probe = self.request(
            "POST",
            f"{base}/run-probe",
            {
                "mutating": self.args.mutating,
                "include_mcp": self.args.include_mcp,
            },
            token=self.token,
            timeout=120.0 if self.args.mutating else 60.0,
        )
        if code != 200:
            self.failures.append(f"POST /run-probe returned {code}: {probe}")
            return {"info": info, "probe": probe}
        if not probe.get("ok"):
            for name in probe.get("failures") or []:
                self.failures.append(f"probe check failed: {name}")
        return {"info": info, "probe": probe}

    def write_cli_audit(self, payload: dict[str, Any]) -> None:
        out_dir = self.data_dir / "smoke-runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"prod-smoke-cli-{int(time.time())}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.notes.append(f"audit log {path}")

    def run(self) -> int:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.load_token()
        self.set_launch_env()

        built = self.build_app()
        if built is None:
            return self.finish()

        app_path = self.install_app(built)
        self.launch_app(app_path)
        if self.failures:
            return self.finish()

        if not self.wait_for_harness(self.args.wait_sec):
            return self.finish()

        print(f"INFO harness={self.harness_base()}")
        print(f"INFO data_dir={self.data_dir}")
        self.report = self.run_harness_probe()
        return self.finish()

    def finish(self) -> int:
        payload = {
            "ok": not self.failures,
            "mutating": self.args.mutating,
            "data_dir": str(self.data_dir),
            "harness": self.harness,
            "report": self.report,
            "notes": self.notes,
            "failures": self.failures,
        }
        if self.args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            for note in self.notes:
                print(f"INFO {note}")
            if self.report.get("probe"):
                probe = self.report["probe"]
                pillars = probe.get("pillars") or {}
                if pillars:
                    print("INFO pillar summary:")
                    for name, state in pillars.items():
                        print(f"  {str(state).upper():4} pillar:{name}")
                for check in probe.get("checks") or []:
                    state = check.get("status") or ("pass" if check.get("ok") else "fail")
                    label = str(state).upper()
                    pillar = check.get("pillar") or "?"
                    level = check.get("level") or "?"
                    print(f"{label} {check.get('name')} [{pillar}/{level}]")
                if probe.get("marker"):
                    print(f"INFO marker={probe.get('marker')}")
            if self.failures:
                for failure in self.failures:
                    print(f"FAIL {failure}")
            else:
                print("PASS production smoke")
        try:
            self.write_cli_audit(payload)
        except OSError as e:
            print(f"WARN could not write audit log: {e}", file=sys.stderr)
        return 1 if self.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA),
        help="Production data dir (default: ~/Library/Application Support/Minion/data)",
    )
    parser.add_argument(
        "--install-path",
        default=str(DEFAULT_INSTALL),
        help="Where to install the smoke copy of Minion.app",
    )
    parser.add_argument("--inbox", default="", help="Optional MINION_INBOX override for launchctl")
    parser.add_argument("--target", default="", help="Optional tauri build target triple")
    parser.add_argument("--wait-sec", type=float, default=180.0, help="Seconds to wait for harness")
    parser.add_argument("--mutating", action="store_true", help="Append dogfood event and verify recall/search")
    parser.add_argument("--include-mcp", action="store_true", help="Verify MCP auth gate during probe")
    parser.add_argument("--no-build", action="store_true", help="Use an existing release build")
    parser.add_argument("--no-install", action="store_true", help="Run built .app in place")
    parser.add_argument("--no-launch", action="store_true", help="Assume Minion.app is already running")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON report")
    args = parser.parse_args()
    return ProdSmoke(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
