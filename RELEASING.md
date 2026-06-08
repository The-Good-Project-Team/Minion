# Releasing & branches — the short version

This is the whole mental model. Read once, you're set.

## Where code lives

- **`main`** = the source of truth. Build from here. It should always equal "the
  latest release, plus any work not yet released."
- **feature branches** (e.g. `hotfix/...`, `feat/...`) = where you work. When
  done, merge into `main`.
- **Never let `main` fall behind a release.** That was the bug that started all
  this: 3.x work sat on a branch, `main` was frozen at the old v2.0.0 "chat-first"
  build, so anyone building `main` got the old chat UI. Fixed now — `main` is at
  v3.2.1.

Rule of thumb: **if you built it and shipped it, it must be on `main`.**

## How a version is named

One place: `desktop/package.json` → `"version"`. The tauri config and the git
tag follow it. Bump that, commit, then release.

## How to cut a release (macOS)

```bash
cd desktop
REPO=goodindustries/Minion \
PUBLISH=1 \
TAURI_SIGNING_PRIVATE_KEY=<key or path> \
TAURI_SIGNING_PRIVATE_KEY_PASSWORD=<password> \
  bash scripts/release_macos.sh
```

That one script does everything:
1. builds both Mac arches (Apple Silicon + Intel)
2. signs them for the auto-updater
3. writes `latest.json` (the file the updater polls)
4. creates the GitHub release `vX.Y.Z` and uploads the binaries + `latest.json`

Leave off `PUBLISH=1` to do a dry build without publishing.

> Set `REPO=goodindustries/Minion` every time — the script's built-in default
> still points at the old repo name. (Worth fixing the default in the script.)

## How auto-update works

The installed app polls
`https://github.com/goodindustries/Minion/releases/latest/download/latest.json`.
When that file names a version newer than the running app, it downloads, checks
the signature against the public key in `tauri.conf.json`, and updates in place.
User data in `~/Library/Application Support/Minion 2` is never touched.

- Builds **before 3.2.1** don't self-update → manual install.
- **3.2.1+** self-update.
- The signing **private key** is required to publish and is NOT in the repo.
  Lose it and you must rotate the public key (as happened once already).

## Tags

The release script tags each release `vX.Y.Z` at the exact commit it built. A tag
== a shipped binary. Don't move a tag after release — `main` simply moves ahead
of the last tag as you add unreleased work. That's normal and healthy.

## Current state (2026-06-08)

- `main` → v3.2.1, includes the white-screen renderer-crash fix.
- Latest published release: **v3.2.1** (first build with a working auto-updater).
- `main` is one commit ahead of the `v3.2.1` tag: a build-script fix + pointing
  the updater endpoint at the canonical `goodindustries` repo. Ships next release.
