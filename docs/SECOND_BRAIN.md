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

See [`docs/LIFE_GRAPH.md`](LIFE_GRAPH.md) (taster-not-chef, life domains) and [`docs/ROADMAP.md`](ROADMAP.md).
