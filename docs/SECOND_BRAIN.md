# Second brain mapping (Minion)

Minion implements the **four-loop** second-brain model (Sources → Knowledge → Tasks → Outputs) as a **butler**: context is composed and pushed proactively; users do not configure cron jobs or maintain kanban backlogs.

## Blueprint → Minion

| Second-brain concept | Minion implementation |
|---------------------|------------------------|
| Sources (raw ingest) | Inbox watcher + macOS screen context + `ambient-ax` indexer |
| Knowledge (wiki) | `wiki_pages` / `wiki_links` SQLite tables; MCP `propose_wiki_update` |
| Tasks | `tasks` table (`origin=inferred\|agent`); Work view (accept/dismiss) |
| Outputs | `outputs` table; MCP `attach_work_output` |
| Today dashboard | `GET /today` + `/today` route |
| Health (silent) | `sync_job_runs`, `system_issues`; NeedsAttention strip when broken |
| Retrieval order | Graph neighborhood first (target), then linked markdown/sources; today MCP uses `working_context` + `ask_minion` |

## Routes (desktop)

- `/activity` — **home**: river of context (observations, parsing, suggestions)
- `/sources` — ingest + search
- `/settings` — consent + support

Legacy routes (`/today`, `/mirror`, `/wiki`, `/work`) redirect to `/activity`.

## Life graph

**Canonical model:** [`docs/LIFE_GRAPH.md`](LIFE_GRAPH.md) — pre-generated scaffold, entity resolution first, graph-neighborhood retrieval.

Every install seeds the **Me → People / Places / Groups / …** tree (`life_graph.py`, `GET /graph/scaffold`). Sources are evidence streams that fill nodes — not separate products.

## Activity feed

`GET /feed` merges ambient events, sync runs, wiki updates, inferred tasks, identity claims, and health issues into one chronological stream with `lane`: `now` | `observed` | `parsed` | `suggestion`.

## MCP

- **Proactive:** `working_context` JSON in initialize instructions
- **Refresh:** `get_working_context`
- **Read/propose:** wiki + work tools (no user-operated task factory)
- **Council only:** user approves/dismisses surfaced suggestions — never "build your empire" setup flows

## Graph-first context

- `GET /graph/context` is the compact bundle for any LLM: graph totals/highlights, next gap, open graph candidates, current focus, recent ambient hints, and optional related memory for `?subject=`.
- `GET /graph/candidates` and `POST /graph/candidates/{id}/resolve` power the approval loop for uncertain merges/facts.
- `GET /menu/status` is the menu-bar contract: pending graph questions, capture health, graph status, and current focus.

## Screen memory

- `POST /screen-memory/remember` runs one screen-memory pass: ambient JSONL -> event log, Accessibility text -> searchable chunks, screenshot fallbacks -> OCR/image ingest, fused `screen_memory_events`, then graph inference queue.
- `GET /screen-memory/search?q=...`, `/summarize-last`, `/what-was-i-doing`, and `/guidance` are the first product surface.
- MCP exposes the same flow through `remember_screen`, `search_screen_memory`, `summarize_last_screen`, `what_was_i_doing`, `screen_guidance`, `screen_memory_status`, and `create_task_from_screen`.
- `GET /screen-memory/events` returns the normalized semantic event stream with trust tiers, visible elements, and user actions.
- `POST /screen-memory/create-task` creates one `origin=screen_memory` inferred task with refs back to the fused screen events.
- Fused screen events are indexed as `screen-event` chunks under `ambient/screen-events/...`, so retrieval can find clipboard summaries, click/key aggregates, visual labels, clip metadata, and Marlin captions.
- Native browser capture emits `dom_snapshot` records for frontmost browser page text and URL when AppleScript/DOM access is available.
- Native capture includes clipboard changes as `clipboard_event` records, capped and deny-list aware, so copied emails can be recalled through screen memory.
- Native input capture emits aggregate-only `mouse_event` / `keyboard_event` records so Minion can answer what was clicked or typed near, without storing keystroke content.
- Fusion carries recent DOM/AX/visual context into user events, so a click near a known button becomes one event with both `mouse_event` and DOM evidence.
- Native macOS capture records bounded 10-30 second rolling `.mov` clips as `rolling_video_clip` records for temporal memory and Marlin adapters.
- External Marlin/OmniParser-style models plug in through `MINION_MARLIN_CMD` / `MINION_OMNIPARSER_CMD`; Minion normalizes their JSON/JSONL stdout into trusted ambient records before fusion.
- `GET /screen-memory/status`, `minion screen-memory-status`, and MCP `screen_memory_status` report collector/adaptor readiness plus recent raw/fused evidence; optional probe mode actively tests `screencapture`, clipboard access without persisting content, and frontmost-app lookup before trusting the memory surface.
- Guidance is graph-first: resolve graph candidates, fill graph gaps, then turn recent screen work into a task or project update.
- Voice/listening is not in the default screen-memory loop. Use screen/AX/OCR first; add OmniParser/Marlin as narrow adapters rather than replacing the local event stream.

See [`docs/LIFE_GRAPH.md`](LIFE_GRAPH.md) (taster-not-chef, life domains) and [`docs/ROADMAP.md`](ROADMAP.md).
