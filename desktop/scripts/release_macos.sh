#!/usr/bin/env bash
set -euo pipefail

# Full macOS release: build both arches → sign updater artifacts → emit
# latest.json → (optionally) publish a GitHub release. This stands up the
# auto-update channel that releases/latest/download/latest.json needs (it has
# been 404 — no latest.json was ever published).
#
# REQUIRES (Tauri updater signing — createUpdaterArtifacts is true in tauri.conf.json):
#   TAURI_SIGNING_PRIVATE_KEY            key contents OR a path to the key file
#   TAURI_SIGNING_PRIVATE_KEY_PASSWORD   the key password ("" if none)
#
# Usage:
#   TAURI_SIGNING_PRIVATE_KEY=... TAURI_SIGNING_PRIVATE_KEY_PASSWORD=... \
#     bash scripts/release_macos.sh            # build + latest.json only (no publish)
#   PUBLISH=1 TAURI_SIGNING_PRIVATE_KEY=... ... bash scripts/release_macos.sh   # also create the GH release
#
# Env knobs:
#   PUBLISH=1     create + upload the GitHub release (default: off, dry build)
#   REPO=owner/name   GitHub repo (default: reif-is-a-foofie/Minion)
#   ARCHES="aarch64 x86_64"   which arches to build (default: both)

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
desktop_dir="$(cd "$here/.." && pwd)"
src_tauri="$desktop_dir/src-tauri"
repo="${REPO:-reif-is-a-foofie/Minion}"
arches="${ARCHES:-aarch64 x86_64}"

if [[ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]]; then
  echo "release: TAURI_SIGNING_PRIVATE_KEY is not set — cannot produce signed updater artifacts." >&2
  echo "        Export the key (contents or a file path) and the password, then re-run." >&2
  exit 2
fi
export TAURI_SIGNING_PRIVATE_KEY
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD="${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}"

version="$(node -p "require('$desktop_dir/package.json').version")"
tag="v$version"
out="$desktop_dir/release/$tag"
mkdir -p "$out"
echo "── Minion release $tag → $out  (repo: $repo)"

# bash 3.2 (macOS default) has no associative arrays — use case helpers.
arch_triple() { case "$1" in aarch64) echo "aarch64-apple-darwin";; x86_64) echo "x86_64-apple-darwin";; esac; }
arch_label()  { case "$1" in aarch64) echo "AppleSilicon";; x86_64) echo "Intel";; esac; }

for arch in $arches; do
  triple="$(arch_triple "$arch")"
  label="$(arch_label "$arch")"
  echo "── building $arch ($triple)…"
  ( cd "$desktop_dir" && bunx tauri build --target "$triple" )

  bundle="$src_tauri/target/$triple/release/bundle/macos"
  app="$(ls -td "$bundle/"*.app 2>/dev/null | head -n1 || true)"
  [[ -n "$app" && -d "$app" ]] || { echo "release: no .app for $arch in $bundle" >&2; exit 3; }
  bash "$here/postbuild_macos_sanitize_app.sh" "$app" || true

  tgz="$(ls -t "$bundle/"*.app.tar.gz 2>/dev/null | head -n1 || true)"
  sig="$(ls -t "$bundle/"*.app.tar.gz.sig 2>/dev/null | head -n1 || true)"
  [[ -f "$tgz" && -f "$sig" ]] || { echo "release: missing signed updater artifact for $arch (key not applied?)" >&2; exit 4; }

  asset="Minion-$version-macOS-$label.app.tar.gz"
  cp "$tgz" "$out/$asset"
  cp "$sig" "$out/$asset.sig"
  # Human-friendly double-clickable zip too (app + install guide).
  bash "$here/package_macos_zip.sh" "$app" "$out/Minion-$version-macOS-$label.zip" || true
done

# Build latest.json (write_latest_json.py lives in the sidecar tree).
wlj="$desktop_dir/scripts/write_latest_json.py"
gen=( python3 "$wlj" --version "$version" --notes "Minion $version — white-screen fix, auto-update, opt-in monitoring" )
for arch in $arches; do
  label="$(arch_label "$arch")"
  asset="Minion-$version-macOS-$label.app.tar.gz"
  url="https://github.com/$repo/releases/download/$tag/$asset"
  if [[ "$arch" == "aarch64" ]]; then
    gen+=( --darwin-aarch64-url "$url" --darwin-aarch64-sig "$out/$asset.sig" )
  else
    gen+=( --darwin-x86_64-url "$url" --darwin-x86_64-sig "$out/$asset.sig" )
  fi
done
"${gen[@]}" > "$out/latest.json"
echo "── wrote $out/latest.json"
cat "$out/latest.json"

if [[ "${PUBLISH:-}" == "1" ]]; then
  echo "── publishing GitHub release $tag…"
  assets=( "$out"/Minion-*.app.tar.gz "$out/latest.json" "$out"/Minion-*.zip )
  if gh release view "$tag" -R "$repo" >/dev/null 2>&1; then
    gh release upload "$tag" "${assets[@]}" -R "$repo" --clobber
  else
    gh release create "$tag" "${assets[@]}" -R "$repo" \
      --title "Minion Two $version" \
      --notes "White-screen renderer-crash fix, working auto-update, opt-in error/crash monitoring. Updating preserves your library (data lives in ~/Library/Application Support/Minion 2)."
  fi
  echo "── verifying updater endpoint…"
  sleep 3
  code="$(curl -s -o /dev/null -w '%{http_code}' -L "https://github.com/$repo/releases/latest/download/latest.json")"
  echo "latest.json endpoint → HTTP $code"
else
  echo "── built locally (no publish). Artifacts in $out"
  echo "   Re-run with PUBLISH=1 to create the GitHub release."
fi
