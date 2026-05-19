#!/usr/bin/env bash
# New Composer session — drop stale paths so we don't verify unrelated files.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
mkdir -p "$HERE"
: >"$HERE/.verify-queue"
exit 0
