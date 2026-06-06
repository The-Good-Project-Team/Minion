#!/usr/bin/env bash
set -euo pipefail

# Package the built macOS app + the install guide into a single distributable
# zip. Unzipping yields a "Minion 2" folder containing:
#   Minion 2.app
#   Install Minion 2.rtf
#
# The guide ships *inside* the zip so a non-technical recipient sees the app and
# the instructions side by side. Run after `tauri build` + the sanitize step.
#
# Usage: package_macos_zip.sh [/path/to/Minion 2.app]
#   With no arg, auto-detects the freshly built bundle.

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
desktop_dir="$(cd "$here/.." && pwd)"
guide="$desktop_dir/packaging/Install Minion 2.rtf"

# --- locate the built .app -------------------------------------------------
app_path="${1:-}"
if [[ -z "$app_path" || ! -d "$app_path" ]]; then
  # productName is "Minion 2", so the bundle is "Minion 2.app". Tolerate older
  # "Minion.app" and the IDE-sandbox temp target dir too. Newest match wins.
  app_path="$(
    {
      ls -td "$desktop_dir/src-tauri/target/release/bundle/macos/"*.app 2>/dev/null
      ls -td /var/folders/*/*/*/cursor-sandbox-cache/*/cargo-target/release/bundle/macos/*.app 2>/dev/null
    } | head -n 1 || true
  )"
fi
if [[ -z "$app_path" || ! -d "$app_path" ]]; then
  echo "package_macos_zip: could not find a built .app (pass the path explicitly)" >&2
  exit 2
fi
if [[ ! -f "$guide" ]]; then
  echo "package_macos_zip: install guide missing: $guide" >&2
  exit 2
fi

echo "packaging app:   $app_path"
echo "packaging guide: $guide"

# --- stage "Minion 2/{app, guide}" and zip the folder ----------------------
stage="$(mktemp -d)"
folder="$stage/Minion 2"
mkdir -p "$folder"
# Always name the app "Minion 2.app" inside the zip, regardless of the bundle's
# on-disk name, so it never collides with the old "Minion.app".
ditto "$app_path" "$folder/Minion 2.app"
cp "$guide" "$folder/Install Minion 2.rtf"

out_zip="$(dirname "$app_path")/Minion 2.zip"
rm -f "$out_zip"
# --keepParent so the archive expands to the "Minion 2" folder, not loose files.
ditto -c -k --sequesterRsrc --keepParent "$folder" "$out_zip"
rm -rf "$stage"

echo "built: $out_zip ($(du -h "$out_zip" | cut -f1))"
