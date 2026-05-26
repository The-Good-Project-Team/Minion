# Life graph (canonical product model)

Minion uses a **pre-generated personal graph scaffold**. Do not start from an empty database.

Memory is not a pile of notes. **Memory is a map of relationships with evidence attached.**

## Taster, not chef

People are **better tasters than chefs**. Most apps make them the chef: empty dashboards, habit builders, cron setup, taxonomy design. That selects for people who are already self-actualized — and they don't need the product.

Minion is the **chef**. The user sits in **grand council**: taste, approve, dismiss, snooze, correct. Never "configure your life OS from scratch."

If someone would set up all their own reminders, relationship cadences, and health tracking, they already do that without us. Real users either **don't know** they should (teach by surfacing) or **know and do** (stay out of the way). Configuration-first fails both groups.

## Leverage, not volume

Top performers in a group of thousands don't do 1000× more work. They do roughly the **same amount** — the **right leverage work**: relationships that compound, health that holds, commitments that matter, decisions that stick.

Minion optimizes for **high-leverage defaults**, not activity volume. Fewer surfaced items, higher signal. Suggestions should answer: *what small move disproportionately improves health, wealth, or relationships right now?*

## Life domains (map everything; user fills nothing)

These are the **aspects of a good life** the graph and routines must cover. Each domain ships with best-practice patterns; evidence streams update them; council actions modify them.

| Domain | Graph home | Leverage routines (pre-seeded, learned over time) |
|--------|------------|---------------------------------------------------|
| **Relationships** | People, Family, Groups | Reach-out when contact drifts; follow-up after new meetings; remember birthdays/ milestones for key people |
| **Work & craft** | Projects, Roles, Obligations | Protect deep work; close loops on commitments; prep before high-stakes meetings |
| **Wealth & assets** | Assets, Organizations, Documents | Renewals, tax-ish deadlines, invoice follow-ups, "waiting on" from others |
| **Health & body** | Preferences, Memory (episodic) | Move regularly; notice weight/trend (opt-in); sleep/recovery patterns when signals exist |
| **Home & household** | Household, Places | Maintenance rhythms; shared calendar coherence |
| **Growth & learning** | Hobbies, Projects, Documents | Finish what you started; revisit notes before they rot |
| **Rest & boundaries** | Preferences, Obligations | Overcommit detection; protect empty calendar |

Routines are **not user-defined cron jobs**. They are **inferred rhythms** from calendar, comms, screen attention, and graph state — confidence rises with evidence; suggestions surface when drift exceeds threshold.

## Learning loop

1. **Observe** — evidence streams update nodes/edges.
2. **Model** — cadence, drift, seasonality per person/project/obligation (conservative defaults first).
3. **Surface** — one suggestion in the activity river with evidence links; council acts.
4. **Adjust** — accept/dismiss/snooze trains thresholds; never silent overwrite of durable memory.

Wrong inference → user rejects once → system learns that neighborhood, not "open settings."

## Council engine (five primitives)

Every leverage action in the activity river uses one extensible contract — not feature-specific schema:

| Primitive | Role |
|-----------|------|
| **Event** | What happened or is coming (with `evidence_refs`) |
| **Proposal** | Ready-to-act artifact (`proposal_type`, `payload`, `intensity`) |
| **Required skill** | Plugin executor (`send_message`, `execute_purchase`, …) |
| **Required info** | Keyed checklist; missing consent → never surface elevated |
| **Approval** | User options (`approve`, `reject`, `snooze`, `edit`) rendered generically |

Pipeline: evidence → pattern detector → proposal builder → required_info gate → feed → user approval → skill bridge → `council_approvals` audit.

**Intensity:** `standard` (e.g. drafted message, **Send?**) vs `elevated` (commerce, **Yes/No**). UI lanes derive from `proposal.intensity` only.

**Learning** (`council_pattern_state`): reject → suppress 30d (×1.5 repeat); snooze → `suppress_until`; approve → tighten `learned_cadence_days`.

Reference patterns (not hardcoded product names): `contact_drift` → `outbound_message`; `date_horizon` + tier → `commerce_action`. New domains = new row in `leverage_patterns.py` + skill plugin.

**API:** `GET /feed` (envelope v2: `item_kind` `council` \| `river`); `POST /council/approve`. MCP attaches up to 2 open proposal titles in `working_context` (no full payloads by default).

**Code:** `council_store.py`, `council_engine.py`, `council_skills.py`, `required_info.py`, `leverage_patterns.py`, `proposal_builder.py`, `council_learning.py`, `entity_resolution.py`, `life_evidence_index.py`, `graph_retrieval.py`; desktop `council_bridges.rs`, `life_evidence.rs`.

## Human primitives

**Primary evidence** is Mac watch/listen: `ambient/stream.jsonl` (focus, all-window AX, browser page text, optional mic, region OCR when AX is empty). Optional imports (exports, calendar drops, files) supplement the graph — they are not the product center.

Typed nodes:

| Type | Role |
|------|------|
| `person` | People you know |
| `household` | Home unit |
| `family` | Family as a unit |
| `place` | Locations |
| `organization` | Companies, institutions |
| `group` | Teams, church, communities |
| `project` | Active work |
| `role` | Named roles |
| `job` | Employment / jobbies |
| `hobby` | Interests |
| `asset` | Things owned |
| `document` | Durable docs |
| `event` | Calendar / meetings |
| `task` | Action items |
| `decision` | Recorded decisions |
| `conversation` | Threads / chats |
| `preference` | Stable prefs |
| `obligation` | Debts, commitments |
| `memory` | Episodic recall |

Each node supports: `id`, `type`, `name`, `aliases`, `summary`, `confidence`, `source_refs`, `created_at`, `updated_at`, `status`, `privacy_level`.

Each edge supports: `from_id`, `to_id`, `relation_type`, `confidence`, `source_refs`, `created_at`, `updated_at`.

Core relation types: `knows`, `related_to`, `married_to`, `parent_of`, `child_of`, `lives_at`, `works_at`, `belongs_to`, `owns`, `manages`, `participates_in`, `responsible_for`, `mentioned_in`, `decided`, `prefers`, `owes`, `waiting_on`, `scheduled_for`.

## Shipped scaffold (stub tree)

Every install seeds an empty tree:

```
Me
├── People
│   ├── Family
│   ├── Friends
│   ├── Work
│   └── Unknown People
├── Places
│   ├── Home
│   ├── Workplaces
│   └── Frequent Places
├── Groups
│   ├── Family
│   ├── Church
│   ├── Teams
│   └── Companies
├── Projects
│   ├── Active
│   ├── Paused
│   └── Archived
├── Work
│   ├── Roles
│   ├── Companies
│   └── Obligations
├── Hobbies
├── Assets
├── Tasks
├── Decisions
└── Preferences
```

## Ingestion order (entity resolution first)

For every source item:

1. Extract entities.
2. Match entities to existing graph nodes.
3. Create missing nodes only when confidence is high enough.
4. Attach source evidence.
5. Update summaries conservatively.
6. Create tasks only when there is an actual action.
7. Never overwrite durable memory without source evidence.

Hard identifiers merge first: email, phone, handle, and iMessage identifiers are normalized before creating people. If the label matches but identifiers conflict, Minion creates a `person_merge` candidate with explicit reasoning instead of silently merging. Approval writes the incoming identifiers, source refs, aliases, and reasoning onto the existing person node; rejection leaves the graph unchanged.

Markdown files remain the **canonical readable layer**. SQLite graph index supports traversal and retrieval.

## Retrieval rule

When the user asks something:

1. Locate the relevant **graph neighborhood** (graph spine + active nodes from recent ambient touches).
2. Read linked markdown / source files.
3. Answer from evidence.

**Graph spine:** `build_graph_spine()` in `graph_ambient.py` composes scaffold fill state, recently touched nodes, and compact markdown for LLM context. Wired into `GET /graph/context`, MCP `working_context`, and `/today`.

**Ambient fill:** each scheduler tick runs `enrich_graph_from_ambient()` — clipboard emails → people, browser hosts → organizations, repeated window titles → active projects, token matches → evidence on existing nodes. Life fills the boxes; 42 only asks when corpus + ambient cannot resolve ambiguity.

**LLM graph mine (core):** when Gemini is configured, `run_graph_mine_tick()` runs on every ambient tick (~2 calls) and 42 scheduler tick (~4 calls), up to ~96/day. The LLM reads embedded corpus evidence and proposes validated graph writes — durable **core** facts (family, birthplace, employers, Me profile) first, then **ongoing active** updates (friends, projects, enrich thin nodes). Python validates citations and confidence before any write. This is the product message: a few dollars of graph intelligence makes the graph the root of all context.

UI surfaces: people I know, projects I'm on, things I owe, things others owe me, places and groups I belong to, recent updates to my world — via the **activity river** (`GET /feed`) and graph sidebar (`GET /graph/scaffold`).

## Ambient sensing (vault-local)

Canonical append-only stream: `<data_dir>/ambient/stream.jsonl` (legacy read fallback: `screen_context/stream.jsonl`).

| Event type | Source | Notes |
|------------|--------|-------|
| `window_focus` / `ax_content_changed` | macOS focus watcher | ~3s poll; foreground; triggers immediate screen-reader tick |
| `window_snapshot` | Screen reader (`screen_reader.rs`) | Event-triggered + ~5s poll; up to 12 visible windows; `ax_nodes`; OCR when AX thin |
| `process_snapshot` | ~60s `ps` sample (opt-in) | Foreground + top CPU apps |
| `app_launched` | Focus change to new app | |
| `browser_visit` | Browser app + page text | Host + URL when Automation allows |
| `dom_snapshot` | Browser Automation + Playwright adapter | URL, page text sample, visible controls; highest trust tier for web UI |
| `clipboard_event` | macOS clipboard watcher | Hash, capped excerpt, detected emails, foreground app/window; deny-list aware |
| `mouse_event` / `keyboard_event` | macOS event tap | Aggregate-only click/key counts and last click point; no keystroke content |
| `rolling_video_clip` | macOS `screencapture -v` | Bounded 10-30s clips under `<data_dir>/ambient/video` for temporal memory |
| `marlin_event` | External Marlin adapter | Timestamped video scene/events; normalized into `time_range` |
| `omniparser_parse` | External OmniParser adapter | Screenshot UI element detections when DOM/AX are weak |
| `listening_*` | Mic / full listening | Opt-in only; WAV → whisper → `ambient-audio` chunks |

Ingest: `ambient_pipeline.py` → `ambient_events`; `screen_memory.py` fuses screen rows into `screen_memory_events`, indexes `screen-event` chunks, extracts high-confidence graph candidates, and queues 42 graph inference. Rollup: `GET /attention/summary`. Per-collector toggles: `settings.json` → `ambient_collectors`.

Voice/listening is **not** part of the default graph-fill loop. The default capture path is screen/DOM/Accessibility/OCR/clipboard/input/video, with voice re-enabled only by explicit opt-in (`MINION_ENABLE_VOICE` for backend ingest and settings for full listening).

**Corpus-first counsel:** `corpus_context.prefetch_for_subject` runs before council proposals; ambient is routing/evidence only.

**Chat:** `GET/POST /chat/*` — graph clarification with corpus excerpts (`graph_clarify.py`).

**Keys:** macOS Keychain search/add via Tauri; `capability_refs.vault_ref` = `keychain:service:account` (`POST /keys/link`).

## Code

- Schema + seed: `chatgpt_mcp_memory/src/store.py`, `life_graph.py`
- Activity river: `activity_feed.py`, `GET /feed`
- Ambient/screen memory: `ambient_pipeline.py`, `screen_memory.py`, `screen_adapters.py`, `attention_rollup.py`; desktop `ambient_collectors.rs`, `screen_reader.rs`, `screen_context.rs`
- Optional listening: `listening_ingest.py`; desktop `listening_session.rs`

### Screen reader (all visible windows)

Foreground-only capture (`screen_context.rs`) logs focus changes and optional screenshots. **Screen reader** complements it: every few seconds it enumerates on-screen windows via Core Graphics, samples Accessibility text per window (title-matched AX subtree), and appends `window_snapshot` lines to `ambient/stream.jsonl`. Non-focused browsers contribute title/metadata only; the frontmost browser also merges AppleScript page text like the focus watcher.

Toggle: `settings.json` → `ambient_collectors.screen_reader` (master: `ambient_sensing_enabled`). Env: `MINION_SCREEN_READER_INTERVAL_SEC` (default 5), `MINION_SCREEN_READER_MAX_WINDOWS` (default 12), `MINION_SCREEN_READER=off` to disable the watcher.

Indexed under `ambient/screen/{date}/` when AX text is present. Phase 2 (not shipped): low-FPS region OCR for AX-empty windows — see `docs/ROADMAP.md`.
- Blueprint mapping: `docs/SECOND_BRAIN.md`
