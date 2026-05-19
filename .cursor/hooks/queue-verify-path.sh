#!/usr/bin/env bash
# Queue absolute paths edited under this repo for consolidated verify on agent `stop`.
set -euo pipefail
command -v jq >/dev/null 2>&1 || exit 0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
QUEUE="$HERE/.verify-queue"

payload="$(cat)"
fp="$(echo "$payload" | jq -r '.file_path // empty')"
[[ -z "$fp" ]] && exit 0

case "$fp" in
"$REPO"/*)
	mkdir -p "$HERE"
	echo "$fp" >>"$QUEUE"
	;;
*) ;; # Outside workspace — ignore
esac

exit 0
