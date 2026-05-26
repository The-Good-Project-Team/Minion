# Screen Memory

Minion's first useful screen feature is memory, not control.

## Layers

1. **Capture**: short-lived screen stream records active app, window title, URL when available, Accessibility text/tree, browser DOM text when available, clipboard changes, and screenshot fallbacks when AX is thin.
2. **Parse**: trust DOM/Accessibility first, then user events, then timestamped video captions, then visual UI parsing, then OCR, then general VLM reasoning.
3. **Fuse**: normalize each source into `ambient_events` and indexed `ambient/*` chunks.
4. **Retrieve**: expose screen memory to graph fill, search, summaries, and local API/MCP clients.

## MVP

- `minion remember-screen`
- `minion search "where did I see the export button?"`
- `minion search "what was I doing in Stripe yesterday?" --app Chrome`
- `minion summarize-last 30m`
- `minion what-was-i-doing`
- `minion guidance`
- `minion screen-memory-status`
- `minion create-task-from-screen`
- `minion verify-screen-memory`

HTTP equivalents:

- `POST /screen-memory/remember`
- `GET /screen-memory/search?q=...`
- `GET /screen-memory/search?q=...&app=Chrome&after=...&before=...`
- `GET /screen-memory/summarize-last?minutes=30`
- `GET /screen-memory/what-was-i-doing?minutes=20`
- `GET /screen-memory/guidance`
- `GET /screen-memory/status?minutes=60`
- `GET /screen-memory/events?minutes=30`
- `POST /screen-memory/create-task`

MCP tools for attached LLMs:

- `remember_screen`
- `search_screen_memory`
- `summarize_last_screen`
- `what_was_i_doing`
- `screen_guidance`
- `screen_memory_status`
- `create_task_from_screen`

The guidance surface is graph-first: resolve graph candidates, fill graph gaps, then turn recent screen work into a task or project update. If there is no graph work and no recent screen evidence, guidance switches to setup mode and points at the strongest blocker, such as `minion screen-memory-status --probe`, `minion screen-memory-permissions`, or adapter env setup.
Task creation writes an `origin=screen_memory` inferred task with context refs back to the fused screen events.
Search can infer simple time windows from the query (`yesterday`, `today`, `last 20m`, `last 2 hours`) and also accepts explicit app and unix timestamp bounds.
High-confidence structured entities from screen memory feed the graph loop. For example, a copied email becomes a `screen_entity` graph candidate with refs back to the fused screen event, so guidance asks who that person is before suggesting generic tasks. Identifier-backed person mentions are included too: named emails such as `Alex Kim <alex@example.com>` preserve the display name, and Messages/iMessage phone or handle evidence is deduped against existing graph people before Minion asks a new question.
Approving a `screen_entity` candidate applies it to the graph: Minion creates or reuses the person node, stores screen identifiers and evidence refs in the node metadata, links the person into the People scaffold, and records a `knows` edge when the approval includes relationship text.
When the same person label appears with conflicting hard identifiers, Minion queues a `person_merge` candidate instead of silently merging. The candidate payload/body includes the visible reasoning (`existing_identifiers`, `incoming_identifiers`, and why confirmation is needed). Approving it writes the incoming email/phone/iMessage/handle/source refs onto the existing person node, preserving the reasoning in node metadata.
The menu-bar status endpoint exposes this as an actionable notification payload: `/menu/status` returns `pending_questions`, `should_notify`, and `next_question` with either the top graph candidate or the next 42 graph gap. The Tauri shell creates a Minion menu-bar icon with Open/Quit actions, starts a native watcher after the sidecar is ready, polls `/menu/status`, updates the icon title with the pending question count, emits `menu://status` to the webview, and sends one deduped local notification per new graph question. The webview listens for that event and shows the same question in a compact top bar, so the notification and in-app chat surface stay aligned. Set `MINION_DISABLE_MENU_BAR_ICON=1` to hide the icon, `MINION_DISABLE_MENU_NOTIFICATIONS=1` to disable notifications, or `MINION_MENU_STATUS_POLL_SEC` to tune the poll interval.

`verify-screen-memory` is the synthetic acceptance check. It uses an isolated temporary DB and stream, writes DOM, click, clipboard, a generated screenshot for real RapidOCR indexing, OCR-shaped screenshot fallback, Marlin-shaped, OmniParser-shaped, and general-VLM-shaped records, then verifies capture ingest, screenshot OCR indexing, semantic fusion, ISO event `time`, top-level temporal refs, confidence tiers including `general_vlm`, OCR fallback tiering, click context carry-forward, deterministic retrieval indexing, video-range retrieval, graph candidate creation, summary, and Miyagi guidance. It does not prove live macOS permissions or real Marlin/OmniParser model environments; use `screen-memory-status --probe` for those.

## Fused Event Shape

`remember-screen` writes raw capture to `ambient_events`, then normalizes it into `screen_memory_events`:

```json
{
  "occurred_at": 1779378186.47,
  "app": "Chrome",
  "window": "Stripe Dashboard",
  "url": "https://stripe.com/dashboard/payouts",
  "scene": "User is working in Chrome: Stripe Dashboard.",
  "visible_elements": [{"role": "button", "label": "Export", "source": "DOM", "confidence": 0.98}],
  "events": [{"type": "user_action", "summary": "User clicked once. Nearby UI target: button 'Export'.", "source": "mouse_event + DOM", "confidence": 0.96}],
  "confidence": 0.96,
  "trust_tier": "user_events"
}
```

Trust tiers are stored as data, not just docs: `dom_or_accessibility`, `user_events`, `temporal_video_events`, `visual_ui_parser`, `ocr`, then `general_vlm`.

## Model Adapters

Do not vendor large model repos into Minion. Keep adapters narrow:

- OmniParser: screenshot UI-element detection when DOM/AX are weak.
- OCR: visible text fallback for screenshot memory.
- Marlin-2B: timestamped event captions and natural-language time-range lookup for rolling video clips.

Voice/listening is not part of the default screen-memory loop. Re-enable it only through an explicit opt-in path.

Browser DOM capture is part of screen memory for frontmost browsers. It emits `dom_snapshot` records with URL, page text sample, simple visible element hints, and the highest trust tier in fusion. Native browser Automation is the low-latency path; Playwright DOM parsing is enabled by default when the shipped script is present. Disable it with `MINION_DISABLE_PLAYWRIGHT_DOM=1` or override it with `MINION_PLAYWRIGHT_DOM_CMD`.

Clipboard capture is part of screen memory. It emits `clipboard_event` records with a content hash, capped text excerpt, detected emails, foreground app/window, and the same ambient deny-list used for screen capture.

Mouse and keyboard capture are first-party but aggregate-only. They emit `mouse_event` and `keyboard_event` records with counts, last click coordinates, foreground app/window, and summaries; keyboard records explicitly set `content_captured=false`.

Fusion walks events chronologically and carries the latest DOM/Accessibility/OmniParser context forward. When a click or key aggregate follows a trusted UI snapshot, the user event inherits nearby visible elements and the action source becomes, for example, `mouse_event + DOM`.

Rolling video capture is an opt-in macOS ambient collector because it requires Screen Recording permission. When enabled, it records main-display `.mov` clips under `<data_dir>/ambient/video/`, defaults to 10 second clips, emits `rolling_video_clip` records with active app/window metadata, and keeps a bounded recent window (`MINION_ROLLING_VIDEO_MAX_CLIPS`, default 24). Tune with `MINION_ROLLING_VIDEO_SECONDS` (10-30) and `MINION_ROLLING_VIDEO_INTERVAL_SECONDS`.

External visual models are adapters, not core dependencies. Set `MINION_MARLIN_CMD` or `MINION_OMNIPARSER_CMD` to a command template and `remember-screen` will run it over recent clips/screenshots, normalize stdout JSON/JSONL into `marlin_event` or `omniparser_parse`, append those records to the ambient stream, and then fuse them into screen memory. Use `{input}` in the command template when the file path belongs somewhere other than the last argv position.
For Playwright DOM parsing, Minion ships `desktop/scripts/playwright-dom-snapshot.mjs`; when enabled, recent browser URLs are parsed into `dom_snapshot` records using the same adapter path. Custom commands may use `{url}` or `{input}`.
Marlin adapter output can use common timestamp fields such as `start_sec`, `end_sec`, `start_time`, `end_time`, `timestamp`, or `duration`; Minion normalizes those into `time_range` and indexes them with the event caption.
See [`SCREEN_ADAPTERS.md`](SCREEN_ADAPTERS.md) for the adapter command contract and setup loop.

`remember-screen` also indexes fused semantic events as `screen-event` chunks under `ambient/screen-events/...`, so search can retrieve clipboard summaries, clicked/typed-near events, DOM/OmniParser element labels, rolling clip metadata, and Marlin captions.
Search hits include structured screen metadata (`screen_event_id`, app/window, trust tier, `time_range`, and `clip_path`) so callers can answer temporal video questions without parsing the rendered event text.
Search responses also include a top-level `video_ranges` array derived from matching hits with `time_range` or `clip_path`, so a query like "when did I export the payout report?" can jump directly to the Marlin segment or rolling clip.

`screen-memory-status` is the live readiness check. It reports collector toggles, whether voice/listening is off, adapter command configuration, recent raw/fused evidence, recent clips/screenshots, total and recent indexed `screen-event` source counts, total and recent screen-memory graph candidate counts, and warnings such as `no_recent_rolling_video_clips` or `marlin_adapter_not_configured`. Persisted `listening` / `full_listening` settings are normalized off unless `MINION_ENABLE_VOICE=1` is explicitly set. It also returns `completion_gates`: pass/blocked/unknown gates for voice-off defaults, collector coverage, live capture probes, recent raw/fused/indexed evidence, recent rolling-video clip evidence, recent graph-fill evidence, and Playwright/Marlin/OmniParser adapters. Add `--probe` to actively test local screenshot capture, a one-second rolling-video capture, Playwright DOM parsing, clipboard access without persisting content, frontmost-app lookup, and configured Marlin/OmniParser/general-VLM commands against one recent input without appending records; capture probes delete their temporary files.

When macOS blocks screenshot or rolling-video capture, the probe returns a
`screen_recording_permission_blocked` warning and a gate detail that says to
grant Screen Recording to Minion/Terminal before retrying. Run
`minion screen-memory-permissions` to open the macOS Screen Recording pane, or
`minion screen-memory-permissions --no-open` to print the settings URL only.
