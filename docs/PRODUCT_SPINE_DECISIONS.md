# Product spine — big-cut decisions (2026-05)

Canonical loop: launch → Minion asks → permissions/sources → connector work → graph questions → context bundle → MCP readers → preferences loop back.

## Keep on spine (now)

- **Graph fill / `forty_two*` modules** — core Q&A; UI brand is **Minion** (`kind: forty_two` feed id unchanged for compat).
- **Context bundle + platform schema** — MCP/local reader contract (`/context/bundle`, `context_platform.py`).
- **Connector intents + resource polls** — onboarding → durable candidates/tasks.
- **Preference promotion** — explicit answers, council feedback, display name.
- **Native capture** — desktop Tauri path; not vendored third-party capture apps.

## Deferred (not deleted this pass)

| Surface | Decision | Rationale |
| ------- | -------- | --------- |
| **Council** | Defer removal | Feed proposals exist; approve/reject UI not on conversation spine yet. Hide in UI until wired. |
| **Wiki / tasks / second_brain** | Defer | Still seed MCP/context; migrate useful bits into graph + bundle before cut. |
| **`/today`** | Defer | Secondary dashboard; collapse into `/feed` + `/context/bundle` later. |
| **Graphify adapter** | Defer | CLI/shadow path used by `bin/minion` and ambient spine; not desktop journey. Mark non-spine. |
| **`graph_clarify` threads** | Defer | Parallel chat path; agent graph-fill is canonical for new UX. |

## Removed (this pass)

- `activity_chat.py` — no HTTP route.
- `/chat/42/*` route aliases — use `/chat/agent/*` only.

## Next cuts (when journeys stay green)

1. Council: ship in-conversation approve/reject or delete scheduler noise.
2. Wiki/tasks: delete after graph/context absorb workflows.
3. Graphify: delete if connector-intent + native mining replace shadow import.
