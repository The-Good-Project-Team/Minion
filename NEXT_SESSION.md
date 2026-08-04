# Next session — Minion contributor handoff

**Date noted:** 2026-06-09  
**Contributor:** Javier Angel (`JavierAngelH`)  
**Repo:** fork `JavierAngelH/Minion` → upstream `goodindustries/Minion`

---

## Already shipped (open for review)

**PR:** https://github.com/goodindustries/Minion/pull/4  
**Branch:** `fix/ui-ingest-progress-and-claude-connect`

Fixes included:
- Ingest progress showing `69/0 learned` (stale `active` counters after batch done)
- Claude Connect marking success without Claude Desktop installed (now errors + real status check)

---

## Tomorrow: fix **Reveal** button (does nothing)

### Symptom

In **Recent sources**, clicking **reveal** has no visible effect.

### Root cause

1. **Add files** / drag-drop uses temporary ingest: `ingestPath(p, false, true)` in `desktop/src/App.tsx`.
2. Sidecar copies file to inbox, indexes it, then **deletes the inbox copy** (`api.py` → `dest.unlink()` when `temporary: true`).
3. DB `sources.path` still points at the **deleted inbox path**.
4. `reveal_in_finder` in `desktop/src-tauri/src/lib.rs` checks `p.exists()` and returns an error if missing.
5. UI calls `void revealInFinder(s.path)` with **no error handling** — failure is silent.

Original file path is tracked in `file_tracking.jsonl` (`chatgpt_mcp_memory/src/file_tracker.py`) but reveal does not use it.

### Proposed fix (smallest correct slice)

1. **Resolve reveal path** before opening Finder:
   - If `source.path` exists → use it.
   - Else look up `original_path` from `file_tracking.jsonl` by matching `staged_path`.
   - Optionally store `original_path` in source `meta` at ingest time (cleaner long-term).

2. **Surface errors in UI** — show a short message when reveal fails (amber text near the row or toast).

### Files likely to touch

| Area | Path |
|------|------|
| Path resolution helper | `chatgpt_mcp_memory/src/file_tracker.py` or new helper in `api.py` |
| API (optional) | `GET /sources/reveal-path?path=...` or enrich `GET /sources` with `reveal_path` |
| UI | `desktop/src/App.tsx` (reveal click handler + error state) |
| Types | `desktop/src/lib/api.ts` |
| Rust (maybe unchanged) | `desktop/src-tauri/src/lib.rs` — if UI resolves path first |

### Verify tomorrow

- [ ] Add a file via **Add files** → click **reveal** → Finder opens **original** file location
- [ ] Inbox-only source (path still exists) → reveal still works
- [ ] Missing file → user sees clear error, not silence
- [ ] `cd chatgpt_mcp_memory && PYTHONPATH=src .venv/bin/python -m pytest tests/test_file_tracker.py -q`
- [ ] `cd desktop && npm run check`

### Quick repro check (Terminal)

```bash
# Pick a source path from the app or API:
curl -s http://127.0.0.1:8765/sources?limit=5 | python3 -m json.tool
ls -la "PASTE_PATH_HERE"   # often "No such file" for temporary ingests
```

---

## Backlog (not tomorrow unless time)

- **Connect Cursor** — one-click MCP setup like Claude (`~/.cursor/mcp.json`); stdio config documented in chat, not implemented in UI.
- **LAN MCP** — `http://127.0.0.1:8765/mcp` with `Authorization: Bearer foofie` for Cursor/other clients.

---

## Dev reminders

```bash
# Free port 1420 if tauri dev fails
pkill -f "vite dev"; pkill -f "minion-desktop"

cd desktop && npm run tauri dev
```

Fork workflow (no push to `origin` directly):

```bash
git push -u fork <branch-name>
gh pr create --repo goodindustries/Minion --head JavierAngelH:<branch-name> --base main
```
