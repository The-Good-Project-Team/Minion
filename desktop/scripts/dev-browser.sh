#!/usr/bin/env bash
# Browser-only Minion UI: Python sidecar + Vite on :1420 (no Tauri compile).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PORT="${MINION_API_PORT:-8765}"
DATA="${MINION_DATA_DIR:-$HOME/Library/Application Support/Minion/data}"
INBOX="${MINION_INBOX:-$HOME/Library/Application Support/Minion/inbox}"
PYTHON="${ROOT}/chatgpt_mcp_memory/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "dev-browser: need ${PYTHON}" >&2
  echo "  cd chatgpt_mcp_memory && uv venv && uv pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p "$DATA" "$INBOX"

if curl -sf "http://127.0.0.1:${PORT}/status" >/dev/null 2>&1; then
  echo "Sidecar already listening on http://127.0.0.1:${PORT}"
else
  echo "Starting sidecar on http://127.0.0.1:${PORT} (data: ${DATA})"
  export MINION_DATA_DIR="$DATA"
  export MINION_INBOX="$INBOX"
  export MINION_DISABLE_WATCHER="${MINION_DISABLE_WATCHER:-1}"
  # Keep ambient graph mining on in browser dev (screen capture still optional).
  export MINION_DISABLE_AMBIENT_SCHEDULER="${MINION_DISABLE_AMBIENT_SCHEDULER:-0}"
  # Optional: export GEMINI_API_KEY=... (or set gemini_api_key in settings.json) for agent dialogue
  export PYTHONPATH="${ROOT}/chatgpt_mcp_memory/src"
  "$PYTHON" "${ROOT}/chatgpt_mcp_memory/src/api.py" --port "$PORT" &
  API_PID=$!
  for _ in $(seq 1 80); do
    if curl -sf "http://127.0.0.1:${PORT}/status" >/dev/null; then
      break
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
      echo "dev-browser: sidecar exited early" >&2
      exit 1
    fi
    sleep 0.25
  done
  if ! curl -sf "http://127.0.0.1:${PORT}/status" >/dev/null; then
    echo "dev-browser: sidecar did not become ready on :${PORT}" >&2
    exit 1
  fi
  echo "Sidecar ready (pid ${API_PID})"
fi

cd "$SCRIPT_DIR/.."
export VITE_BROWSER_DEV=true
export VITE_E2E_API_PORT="$PORT"

echo ""
echo "  UI:  http://127.0.0.1:1420/"
echo "  API: http://127.0.0.1:${PORT}/"
echo ""

exec npm run dev -- --host 127.0.0.1 --port 1420
