#!/usr/bin/env bash
# Full verification harness: Python core + desktop types + unit + Playwright butler UI.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "== Python pytest =="
cd "$ROOT/chatgpt_mcp_memory"
export MINION_DISABLE_AMBIENT_SCHEDULER=1
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q --tb=short

echo "== Rust cargo test =="
cd "$ROOT/desktop/src-tauri"
cargo test

echo "== Desktop svelte-check =="
cd "$ROOT/desktop"
npm run check

echo "== Desktop vitest =="
npm run test:unit

echo "== Playwright butler UI (install chromium if needed) =="
npx playwright install chromium
export MINION_DISABLE_AMBIENT_SCHEDULER=1
npm run test:e2e

echo "== verify-all OK =="
