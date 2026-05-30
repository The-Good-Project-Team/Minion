#!/usr/bin/env bash
# Idempotent sidecar launcher: reuse an already-serving :PORT, else start api.py.
# Safe to run under a preview/launch manager — it never double-binds the port.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT="${MINION_API_PORT:-8765}"
PYTHON="${PKG}/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "run-sidecar: need ${PYTHON}" >&2
  echo "  cd chatgpt_mcp_memory && uv venv && uv pip install -r requirements.txt" >&2
  exit 1
fi

if curl -sf "http://127.0.0.1:${PORT}/status" >/dev/null 2>&1; then
  echo "[minion-api] reusing sidecar already listening on http://127.0.0.1:${PORT}"
  # Stay in the foreground so the launch manager keeps a live process and
  # surfaces when the reused sidecar goes away.
  while curl -sf "http://127.0.0.1:${PORT}/status" >/dev/null 2>&1; do
    sleep 5
  done
  echo "[minion-api] reused sidecar on :${PORT} is gone" >&2
  exit 1
fi

export PYTHONPATH="${PKG}/src"
exec "$PYTHON" "${PKG}/src/api.py" --port "$PORT"
