# Screen Memory Completion Audit

Objective: make Minion screen-memory-first, with voice/listening off by default,
full screen capture/parsing/fusion/retrieval, graph fill, and Miyagi-style
"do this" guidance. Memory is the MVP; computer control is out of scope.

Date of latest audit: 2026-05-21.

## Success Criteria

| Requirement | Evidence | Status |
| --- | --- | --- |
| Voice/listening dropped from default loop | `settings.py` defaults `listening=false`, `full_listening=false` and normalizes older persisted voice settings off unless `MINION_ENABLE_VOICE=1`; Rust desktop collector/full-listening checks use the same env opt-in; `ambient_scheduler.py` only ingests listening when `MINION_ENABLE_VOICE` is truthy; live `screen-memory-status` now reports `voice_default_off=true` even with legacy settings that had voice enabled. | Shipped |
| First-party capture includes app/window/URL/AX/DOM/screenshots | `screen_context.rs`, `screen_reader.rs`, and `ambient_pipeline.py` emit and ingest `window_focus`, `ax_content_changed`, `window_snapshot`, `browser_visit`, `dom_snapshot`, and `screenshot_fallback`. | Shipped |
| Clipboard capture | `ambient_collectors.rs` emits `clipboard_event` with hash, capped excerpt, detected emails, foreground app/window, and deny-list checks. | Shipped |
| Mouse/keyboard events without keystroke content | `ambient_collectors.rs` emits aggregate-only `mouse_event` / `keyboard_event`; tests assert keyboard content is not captured. | Shipped |
| Rolling 10-30s screen clips | `ambient_collectors.rs` uses `/usr/sbin/screencapture -v -V<duration>`, writes bounded `.mov` clips, and emits `rolling_video_clip`. | Shipped |
| Playwright/DOM parser | `desktop/scripts/playwright-dom-snapshot.mjs` plus `screen_adapters.py` default-on `playwright_dom` adapter emits `dom_snapshot`. Live probe found `Export` in a test DOM. | Shipped |
| Native macOS Accessibility parser | AX samples from screen context/screen reader are indexed and fused as `dom_or_accessibility`. | Shipped |
| OCR fallback | `remember-screen` ingests screenshot fallbacks through existing image/OCR ingest path. | Shipped |
| OmniParser adapter | `MINION_OMNIPARSER_CMD` contract, `omniparser_json_adapter.py`, fusion into `visual_ui_parser`, and adapter probe exist. | Code shipped; real model not configured locally |
| Marlin-2B adapter | `MINION_MARLIN_CMD` contract, `marlin_hf_adapter.py`, temporal normalization into `time_range`, fusion into `temporal_video_events`, adapter probe, and `screen_search.video_ranges` for natural-language clip/time retrieval exist. | Code shipped; real model not configured locally |
| General VLM fallback adapter | `MINION_GENERAL_VLM_CMD` contract, `general_vlm_json_adapter.py`, adapter status/probe, ambient ingest, and fusion into `general_vlm` exist as the lowest-trust screenshot fallback. | Code shipped; real model optional/not configured locally |
| Confidence hierarchy stored in data | `screen_memory.py` emits trust tiers: `dom_or_accessibility`, `user_events`, `temporal_video_events`, `visual_ui_parser`, `ocr`, `general_vlm`; `ambient_pipeline.py` ingests all tier source kinds; `verify-screen-memory` synthesizes and checks all six. | Shipped |
| Fused semantic event stream | `store.py` creates `screen_memory_events`; `screen_memory.py` writes ISO `time`, numeric `occurred_at`, visible elements, actions, refs, confidence, trust tier, top-level `time_range`/`clip_path`, and raw evidence. | Shipped |
| Vector/FTS retrieval | Fused events index as `screen-event` chunks under `ambient/screen-events/...`; search returns text, structured metadata, and top-level `video_ranges` for matching Marlin/clip evidence. | Shipped |
| Queries: Stripe/yesterday/app filters | `screen_search` infers `yesterday`, `today`, and `last N`; accepts `app`, `after`, `before`; tests cover app + yesterday filter. | Shipped |
| Query: copied investor email | Clipboard events index excerpts/detected emails; screen entity extraction creates `screen_entity` graph candidates, preserves display names around emails, and dedupes phone/iMessage identifiers against existing graph people. Same-name conflicting identifiers create `person_merge` candidates with visible reasoning, and approval merges incoming identifiers/evidence into the existing graph person. | Shipped |
| Query: clicked button after sheet/open page | Fusion carries recent DOM/AX/OmniParser context into mouse/keyboard events so click summaries include nearby targets. | Shipped |
| Summarize last N minutes | `summarize_last`, API, CLI, and MCP tool exist. | Shipped |
| What-was-I-doing | `what_was_i_doing`, API, CLI, and MCP tool exist. | Shipped |
| Create task from recent screen | `create_task_from_recent_screen`, API, CLI, and MCP tool create `origin=screen_memory` tasks with event refs. | Shipped |
| Graph fill emphasis | `remember_screen` queues graph inference; screen emails, named emails, and Messages/iMessage identifiers become graph candidates when unknown; approving a `screen_entity` candidate creates/reuses the graph person with evidence refs; approving a `person_merge` candidate writes incoming identifiers/source refs onto the existing person; `miyagi_guidance` prioritizes candidates, then graph gaps, then recent screen tasks. When no graph work or screen evidence exists, guidance switches to setup mode and names the strongest readiness blocker. | Shipped |
| Menu/status readiness | `/screen-memory/status`, `minion screen-memory-status`, and MCP `screen_memory_status` report collectors, adapters, evidence counts, recent rolling-video clip evidence, total/recent indexed `screen-event` source counts, total/recent screen-memory graph candidate counts, readiness warnings, probes, and first-class `completion_gates` for pass/blocked/unknown setup state. The indexed, rolling-clip, and graph-fill gates require recent evidence, not just historical sources/candidates. `/menu/status` reports `should_notify` and `next_question` for graph candidates/gaps; the Tauri shell creates a menu-bar icon, updates its pending-count title, emits `menu://status`, sends deduped local notifications, and the webview renders the same question in a top bar. | Shipped |
| MVP CLI commands | `bin/minion`: `remember-screen`, `search`, `summarize-last`, `what-was-i-doing`, `guidance`, `screen-memory-status`, `create-task-from-screen`, `verify-screen-memory`. Screen-memory commands run from this checkout use `chatgpt_mcp_memory/.venv` without requiring `--workspace`; CLI search preserves `video_ranges`. | Shipped |
| API/MCP surfaces | FastAPI `/screen-memory/*`, `/graph/context`, `/graph/candidates`; MCP `remember_screen`, `search_screen_memory`, `summarize_last_screen`, `what_was_i_doing`, `screen_guidance`, `screen_memory_status`, `create_task_from_screen`. API and MCP search preserve `video_ranges`; desktop API types expose typed screen search hits and video ranges. | Shipped |

## Verification Evidence

Latest successful checks:

- Focused screen adapter + memory suite after `completion_gates`: `18 passed`
- Focused graph/screen/MCP/entity suite after identifier-backed screen graph fill: `26 passed`
- Focused graph candidate resolver suite: `25 passed`
- Broad graph/screen/MCP/adapter suite after verifier + OCR fallback tiering: `34 passed`
- Broad graph/screen/MCP/adapter suite after `person_merge` graph-fill application: `35 passed`
- Broad graph/screen/MCP/adapter/bin suite after repo-local CLI resolver coverage: `38 passed`
- Broad graph/screen/MCP/adapter/bin suite after MCP `video_ranges` contract coverage: `39 passed`
- Broad graph/screen/MCP/API/adapter/bin suite after API/desktop `video_ranges` contract coverage: `40 passed`
- Broad graph/screen/MCP/API/CLI/adapter/bin suite after CLI `video_ranges` contract coverage: `41 passed`
- Broad graph/screen/MCP/API/CLI/adapter/bin suite after real `general_vlm` ingest/fusion, optional adapter support, graph/index/rolling-clip gate tightening, failed-probe detail repair, capture-permission hints, setup guidance, and recent graph-candidate gating: `60 passed`
- Screen-memory status gate tightening: `tests/test_screen_memory.py` passed `21 passed`; synthetic verifier returned `ok=true`, reported `2 recent / 2 total indexed screen-event sources`, reported `1 open / 1 recent / 1 total graph candidates from screen evidence`, and correctly blocked completion gates on `0 recent rolling video clips`
- Voice-off legacy settings migration: `tests/test_analytics_remote.py tests/test_screen_memory.py` passed `23 passed`; desktop Rust `cargo test` passed `10 passed`; escalated live status against `/Users/reify/Library/Application Support/Minion/data` reports `voice_default_off=true` despite legacy persisted `listening=true`
- Focused Python contract suite including settings, screen memory, adapters, API, MCP, entity resolution, graph context, and graph fill: `60 passed`
- Permission remediation CLI: `./bin/minion screen-memory-permissions --no-open` prints the Screen Recording instructions and the macOS settings URL; `tests/test_bin_minion.py` covers both no-open and macOS-open paths.
- Focused adapter/wrapper/memory suite after `MINION_GENERAL_VLM_CMD`: `23 passed`; focused adapter/wrapper slice: `8 passed`
- Desktop unit: `2 passed`
- Desktop Rust: `6 passed` with one existing dead-code warning
- Desktop type check after resolver API type update: `0 errors`, one existing Svelte `<slot>` deprecation warning
- Desktop Rust after menu-bar icon + notification watcher: `9 passed` with one existing dead-code warning
- Desktop type check after in-app question bar: `0 errors`, one existing Svelte `<slot>` deprecation warning
- Synthetic screen-memory verifier: `verify-screen-memory` returned `ok=true` with 13/13 checks passing, including real RapidOCR screenshot indexing, ISO event `time`, top-level temporal refs, all confidence tiers including `general_vlm`, OCR fallback tiering, and `video_range_retrieval`; the verifier now ingests/fuses 7 synthetic ambient events across all six trust tiers
- Repo-local CLI smoke: `./bin/minion screen-memory-status` runs without `--workspace`, reports `voice_default_off=true`, and surfaces current readiness blockers instead of failing on the default home workspace path; `tests/test_bin_minion.py` covers the repo-local workspace resolver
- Latest full backend before the screen-memory adapter/fusion expansion: `129 passed`
- Full backend rerun after adapter/fusion expansion was not repeated in this pass; earlier full reruns were blocked by sidecar-test sandbox/escalation limits, so the current evidence is the focused 44-test contract slice plus desktop checks.

Current scratch live probe from `./bin/minion screen-memory-status --probe --derived-dir /private/tmp/minion-live-probe`:

- Voice default off: true
- Collectors configured: pass
- Clipboard probe: ok, content not persisted
- Screenshot probe: failed, `could not create image from display 0`
- Rolling video probe: failed, `dispatch_source_create returned NULL...`
- Playwright DOM probe: failed, headless browser closed during launch
- Frontmost app probe: failed, AppleScript `-10827`
- Recent ambient/fused/indexed/graph evidence: 0 in the scratch data dir

Current live data-dir probe from `./bin/minion screen-memory-status --probe --derived-dir "$HOME/Library/Application Support/Minion/data"`:

- Voice default off: true
- Collectors configured: pass
- Clipboard probe: ok, content not persisted
- Frontmost app lookup: ok
- Playwright DOM probe: ok, title `MinionProbe`, visible element count `1`
- Screenshot probe: failed, `could not create image from display 2077749241`
- Rolling video probe: failed, `screencapture: capture error The operation could not be completed`
- Gate detail now identifies this as macOS blocking screenshot/rolling-video capture and says to grant Screen Recording to Minion/Terminal or run `minion screen-memory-permissions` before retrying; readiness includes `screen_recording_permission_blocked`
- Recent ambient/fused/indexed/graph evidence: 0 in the live data dir
- Marlin/OmniParser: not configured

Earlier GUI-session live macOS probe evidence from `screen-memory-status --probe` (not re-run in this pass):

- Screenshot capture: ok
- One-second rolling video capture: ok
- Playwright DOM parser: ok, visible element count `1`, title `MinionProbe`
- Clipboard access: ok, content not persisted
- Frontmost app lookup: ok
- Voice default off: true

## Remaining Blockers

These are external setup blockers, not missing in-repo code paths:

1. Real Marlin-2B environment is not installed/configured locally.
   - Required signal: `MINION_MARLIN_CMD` set and `screen-memory-status --probe` shows `adapter_commands.marlin.ok=true`.
2. Real OmniParser environment is not installed/configured locally.
   - Required signal: `MINION_OMNIPARSER_CMD` set and `screen-memory-status --probe` shows `adapter_commands.omniparser.ok=true`.
3. The scratch probe directory has no recent live ambient stream data.
   - Required signal: run the desktop app long enough to emit current `ambient/stream.jsonl`, then `remember-screen`, then status shows recent raw/fused/indexed screen evidence.

Do not mark this objective complete until the three signals above are true.
