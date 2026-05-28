# Testing strategy (user objectives)

Tests prove **durable user outcomes**, not JSON shape or label visibility alone.

## Keep

| Layer | What | Why |
| ----- | ---- | --- |
| Playwright `e2e/journeys.spec.ts` | First-run onboarding, connector poll → task, graph Q&A | Full UI loop |
| Python integration | ingest→search, graph_fill, connector_intent, preference_promotion, context_platform, MCP privacy | Product spine |
| `test_chat_sse.py` | Agent SSE stream + graph reply | Durable chat path |

## Converted / renamed

- Shallow `/chat/42/*` HTTP checks → `/chat/agent/*` (canonical).
- API smoke onboarding block includes profile persistence.

## Deleted / merged

- `test_activity_chat.py` — orphan module, no route.
- `e2e/butler-ui.spec.ts` — duplicate shell checks (journeys replace).
- Duplicate Playwright heading/placeholder-only specs.

## E2E helpers

- `MINION_E2E=1` (set by `desktop/scripts/run-e2e-stack.sh`) enables `POST /dev/e2e/seed-graph-gap` for graph journey seeding only.

## Success bar

A change is verified when at least one check proves: Playwright journey, backend DB/MCP effect, or type/unit guardrail — journeys are the release criteria.
