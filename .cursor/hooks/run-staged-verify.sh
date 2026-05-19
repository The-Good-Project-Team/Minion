#!/usr/bin/env bash
# Run tiered checks once per agent stop for files touched this turn (debounced queue).
# Fail-open: never blocks Cursor; results append to verify-last.log and stderr summary.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
QUEUE="$HERE/.verify-queue"
LOG="$HERE/verify-last.log"

if [[ "${MINION_HOOK_VERIFY:-1}" == "0" ]]; then
	exit 0
fi

cat >>"$LOG" <<EOF

===== $(date -Iseconds) stop-hook =====
EOF

if [[ ! -f "$QUEUE" ]] || [[ ! -s "$QUEUE" ]]; then
	echo "[minion-hook] verify-queue empty — nothing to run." | tee -a "$LOG" >&2
	echo "{}"
	exit 0
fi

paths="$(sort -u "$QUEUE")"
: >"$QUEUE"

need_py=false
need_rs=false
need_fe=false

while IFS= read -r p; do
	[[ -z "$p" ]] && continue
	if [[ "$p" == "$REPO/chatgpt_mcp_memory/"* ]] && [[ "$p" == *.py ]]; then
		need_py=true
	fi
	if [[ "$p" == "$REPO/desktop/src-tauri/"* ]] && [[ "$p" == *.rs ]]; then
		need_rs=true
	fi
	if [[ "$p" == "$REPO/desktop/"* ]]; then
		ext="${p##*.}"
		case "$ext" in
		ts | tsx | svelte | mjs | cjs | js) need_fe=true ;;
		esac
	fi
done <<<"$paths"

echo "[minion-hook] staged paths → py=$need_py rust=$need_rs frontend=$need_fe" | tee -a "$LOG" >&2

py_ec=0
rs_ec=0
fe_ec=0
unit_ec=0

if [[ "$need_py" == true ]]; then
	PY="$REPO/chatgpt_mcp_memory/.venv/bin/python"
	if [[ -x "$PY" ]]; then
		echo "--- pytest ---" >>"$LOG"
		(cd "$REPO/chatgpt_mcp_memory" && PYTHONPATH=src "$PY" -m pytest tests/ -q --tb=line >>"$LOG" 2>&1) || py_ec=$?
	else
		echo "--- pytest skipped (no chatgpt_mcp_memory/.venv) ---" | tee -a "$LOG" >&2
	fi
fi

if [[ "$need_rs" == true ]]; then
	echo "--- cargo test (desktop/src-tauri) ---" >>"$LOG"
	(cd "$REPO/desktop/src-tauri" && cargo test >>"$LOG" 2>&1) || rs_ec=$?
fi

if [[ "$need_fe" == true ]]; then
	if [[ -d "$REPO/desktop/node_modules" ]]; then
		echo "--- npm run check ---" >>"$LOG"
		(cd "$REPO/desktop" && npm run check >>"$LOG" 2>&1) || fe_ec=$?
		echo "--- npm run test:unit ---" >>"$LOG"
		(cd "$REPO/desktop" && npm run test:unit >>"$LOG" 2>&1) || unit_ec=$?
	else
		echo "--- frontend checks skipped (no desktop/node_modules) ---" | tee -a "$LOG" >&2
	fi
fi

echo "[minion-hook] exit codes py=$py_ec rust=$rs_ec check=$fe_ec vitest=$unit_ec · full log: .cursor/hooks/verify-last.log" | tee -a "$LOG" >&2

echo "{}"
exit 0
