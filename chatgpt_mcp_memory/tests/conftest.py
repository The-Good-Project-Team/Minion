"""
Shared pytest fixtures: spin up a real sidecar on a free port against a
scratch data dir / inbox, expose a tiny typed client, tear down cleanly.

We spawn the same `api.py` the Tauri shell spawns so the tests exercise the
whole HTTP + WebSocket stack (uvicorn, FastAPI, pydantic, CORS, lifespan) --
not a TestClient mock. Slightly slower, but catches wiring bugs that only
show up over the wire.

E2E requires the same interpreter capability as production ``store.connect``
(loadable SQLite extensions + ``sqlite-vec``). A session preflight fails fast
with setup hints if ``python3`` is wrong; use ``chatgpt_mcp_memory/.venv/bin/python``.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
API_SCRIPT = REPO_ROOT / "chatgpt_mcp_memory" / "src" / "api.py"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _e2e_sqlite_environment_error() -> Optional[str]:
    """Same capability ``store._load_vec_extension`` needs; return message if missing."""
    try:
        import sqlite_vec
    except ImportError:
        return (
            "Package `sqlite-vec` is not installed.\n"
            "  cd chatgpt_mcp_memory && uv pip install -r requirements.txt\n"
        )
    conn = None
    try:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        if not hasattr(conn, "enable_load_extension"):
            return (
                f"This interpreter cannot load SQLite extensions:\n  {sys.executable}\n"
                "macOS system Python often lacks ``enable_load_extension``. "
                "Use the project venv (Python 3.11+ from uv or Homebrew):\n"
                "  cd chatgpt_mcp_memory && uv venv --python 3.11\n"
                "  uv pip install -r requirements.txt -r requirements-dev.txt\n"
                "  .venv/bin/python -m pytest tests/ -q\n"
            )
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception as e:
        return f"sqlite_vec / SQLite preflight failed: {e!r}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail fast when the env cannot run Minion (wrong Python or missing deps)."""
    if getattr(session.config.option, "collectonly", False):
        return
    if os.environ.get("MINION_SKIP_E2E_PREFLIGHT", "").strip() == "1":
        return
    err = _e2e_sqlite_environment_error()
    if err:
        pytest.exit(
            "\n\n=== Minion API e2e: environment check failed ===\n\n"
            + err
            + "\nUntil this check passes, red tests only tell you the toolchain is wrong.\n"
            "After it passes, failing tests mean Minion code or tests need fixing.\n"
            "Emergency bypass (not recommended): MINION_SKIP_E2E_PREFLIGHT=1\n",
            returncode=1,
        )


def _pick_free_port() -> int:
    """Ask the kernel for a port, then close the socket so the sidecar can bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class FakeGeminiServer:
    base_url: str
    response_text: str
    requests: List[Dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def fake_gemini(tmp_path: Path):
    """Real local HTTP Gemini-compatible endpoint; no function patching."""
    servers: List[ThreadingHTTPServer] = []
    old_key = os.environ.get("GEMINI_API_KEY")
    old_base = os.environ.get("MINION_GEMINI_API_BASE")
    old_disable_files = os.environ.get("MINION_GEMINI_DISABLE_SECRET_FILES")

    def start(response_text: str) -> FakeGeminiServer:
        state = FakeGeminiServer(base_url="", response_text=response_text)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = {}
                state.requests.append({"path": self.path, "payload": payload})
                if "streamGenerateContent" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for part in state.response_text.split("|"):
                        event = {
                            "candidates": [
                                {"content": {"parts": [{"text": part}]}, "finishReason": "STOP"}
                            ]
                        }
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    return
                body_out = {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": state.response_text}]},
                            "finishReason": "STOP",
                        }
                    ]
                }
                raw = json.dumps(body_out).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args: Any) -> None:
                return

        port = _pick_free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        state.base_url = f"http://127.0.0.1:{port}/v1beta/models"
        os.environ["GEMINI_API_KEY"] = "fake-local-key"
        os.environ["MINION_GEMINI_API_BASE"] = state.base_url
        os.environ["MINION_GEMINI_DISABLE_SECRET_FILES"] = "1"
        return state

    try:
        yield start
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        if old_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = old_key
        if old_base is None:
            os.environ.pop("MINION_GEMINI_API_BASE", None)
        else:
            os.environ["MINION_GEMINI_API_BASE"] = old_base
        if old_disable_files is None:
            os.environ.pop("MINION_GEMINI_DISABLE_SECRET_FILES", None)
        else:
            os.environ["MINION_GEMINI_DISABLE_SECRET_FILES"] = old_disable_files


@dataclass
class SidecarClient:
    base: str
    port: int
    data_dir: Path
    inbox: Path
    process: subprocess.Popen
    claude_cfg_path: Path

    @property
    def ws_url(self) -> str:
        return self.base.replace("http://", "ws://") + "/events"

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.get(self.base + path, timeout=15.0, **kwargs)

    def post(self, path: str, json_body: Optional[Dict[str, Any]] = None, **kwargs: Any) -> httpx.Response:
        return httpx.post(self.base + path, json=json_body, timeout=60.0, **kwargs)

    def put(self, path: str, json_body: Optional[Dict[str, Any]] = None, **kwargs: Any) -> httpx.Response:
        return httpx.put(self.base + path, json=json_body, timeout=30.0, **kwargs)

    def delete(self, path: str, json_body: Optional[Dict[str, Any]] = None, **kwargs: Any) -> httpx.Response:
        return httpx.request(
            "DELETE", self.base + path, json=json_body, timeout=15.0, **kwargs
        )

    def patch(self, path: str, json_body: Optional[Dict[str, Any]] = None, **kwargs: Any) -> httpx.Response:
        return httpx.patch(self.base + path, json=json_body, timeout=15.0, **kwargs)

    def wait_for_sources(self, expected_min: int, timeout: float = 30.0) -> List[Dict[str, Any]]:
        """Poll /sources until we see at least `expected_min` sources."""
        deadline = time.monotonic() + timeout
        last: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            r = self.get("/sources")
            r.raise_for_status()
            last = r.json()["sources"]
            if len(last) >= expected_min:
                return last
            time.sleep(0.25)
        return last


def _spawn_sidecar(
    data_dir: Path,
    inbox: Path,
    port: int,
    claude_cfg_path: Path,
) -> subprocess.Popen:
    """Spawn api.py with the test env. Use the interpreter running pytest so
    we inherit the same venv (which must have fastapi/uvicorn installed)."""
    env = os.environ.copy()
    env["MINION_DATA_DIR"] = str(data_dir)
    env["MINION_INBOX"] = str(inbox)
    env["MINION_DISABLE_WATCHER"] = "1"  # tests drive ingest explicitly
    env["MINION_DISABLE_AMBIENT_SCHEDULER"] = "1"
    env["MINION_DISABLE_PLAYWRIGHT_DOM"] = "1"
    env["CLAUDE_DESKTOP_CONFIG"] = str(claude_cfg_path)
    env["MINION_SKIP_CLAUDE_APP_CHECK"] = "1"
    env["PYTHONPATH"] = str(API_SCRIPT.parent) + os.pathsep + env.get("PYTHONPATH", "")

    process = subprocess.Popen(
        [sys.executable, str(API_SCRIPT), "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    # Drain stdout continuously so the child cannot block on a full pipe.
    # Keep a bounded tail for debugging when startup fails.
    output_tail: List[str] = []

    def _drain() -> None:
        if not process.stdout:
            return
        for line in iter(process.stdout.readline, ""):
            output_tail.append(line)
            if len(output_tail) > 2000:
                del output_tail[:500]

    t = threading.Thread(target=_drain, name="minion-sidecar-stdout", daemon=True)
    t.start()
    setattr(process, "_minion_output_tail", output_tail)
    return process


def _wait_ready(base: str, process: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Optional[str] = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            # The sidecar died -- dump whatever it emitted before giving up.
            out = ""
            tail = getattr(process, "_minion_output_tail", None)
            if tail:
                out = "".join(tail)
            elif process.stdout:
                out = process.stdout.read()
            raise RuntimeError(
                f"sidecar exited early (code={process.returncode}) before /status was ready.\n--- output ---\n{out}"
            )
        try:
            r = httpx.get(base + "/status", timeout=1.0)
            if r.status_code == 200:
                return
            last_err = f"status={r.status_code}"
        except Exception as e:  # pragma: no cover - timing dependent
            last_err = str(e)
        time.sleep(0.25)
    raise RuntimeError(f"sidecar not ready after {timeout}s: {last_err}")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sidecar(tmp_path: Path) -> SidecarClient:
    """Per-test sidecar -- each test gets a fresh DB, inbox, and Claude config.

    Scoped per-test (not session) because several tests mutate the DB in
    ways that would cross-contaminate (e.g. delete removes chunks the next
    test needs). With MINION_DISABLE_WATCHER=1 + a small fixture set the
    startup cost (~2s) is acceptable.
    """
    data_dir = tmp_path / "data"
    inbox = tmp_path / "inbox"
    data_dir.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    claude_cfg = tmp_path / "claude_desktop_config.json"
    port = _pick_free_port()
    base = f"http://127.0.0.1:{port}"

    process = _spawn_sidecar(data_dir, inbox, port, claude_cfg)
    try:
        _wait_ready(base, process)
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        raise

    client = SidecarClient(
        base=base,
        port=port,
        data_dir=data_dir,
        inbox=inbox,
        process=process,
        claude_cfg_path=claude_cfg,
    )

    try:
        yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


@pytest.fixture
def staged_note(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Copy note.md out to a tmp path so /ingest's copy-into-inbox step has
    a source outside the inbox to work with (same shape as real user input)."""
    src = fixtures_dir / "note.md"
    dest = tmp_path / "staged-note.md"
    shutil.copy2(src, dest)
    return dest


@pytest.fixture
def staged_project(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Copy the fixture project tree out to a tmp dir so each test gets a
    pristine source (we copy-into-inbox; we don't want to touch the
    committed fixtures)."""
    src = fixtures_dir / "project"
    dest = tmp_path / "staged-project"
    shutil.copytree(src, dest)
    return dest
