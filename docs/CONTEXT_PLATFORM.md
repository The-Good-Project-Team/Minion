# Minion Context Platform

Canonical architecture for the product spine. Complements [LIFE_GRAPH.md](LIFE_GRAPH.md) (graph model) and [ROADMAP.md](ROADMAP.md) (themes).

## Product promise

Minion stores your context **locally**, turns it into a **private model** of your work and life, and serves only the **right slice** to the right agent at the right moment.

**Raw context is not the product.** Rationalized, permissioned, live context is.

## Four layers

| Layer | Role | Shipped anchors |
| ----- | ---- | ---------------- |
| **Vault** | Ingest, chunk, embed, index evidence on disk | `chatgpt_mcp_memory/src/ingest.py`, `store.py`, `watcher.py`, `ambient_pipeline.py`, `screen_memory.py`, `life_evidence_index.py` |
| **Context server** | MCP + HTTP expose scoped projections | `mcp_server.py`, `api.py`, `consent_policy.py`, `retrieval_bias.py` |
| **World model** | Life/work graph: people, projects, obligations, decisions | `life_graph.py`, `graph_fill.py`, `graph_ambient.py`, `graph_corpus_mine.py`, `entity_resolution.py`, `graph_context.py` |
| **Live preferences** | Current stance from answers, council, identity, ambient | `identity.py`, `preference_promotion.py`, `retrieval_bias.py`, `context_core.py` |

## Data flow

```mermaid
flowchart TB
  sources[UserSources] --> vault[Vault]
  vault --> encode[EncodeIndex]
  encode --> retrieval[Retrieval]
  encode --> graph[WorldModel]
  graph --> prefs[LivePreferences]
  retrieval --> policy[ConsentScopes]
  graph --> policy
  prefs --> policy
  policy --> mcp[MCPReaders]
  policy --> desktop[DesktopUI]
  desktop --> corrections[UserCorrections]
  corrections --> graph
  corrections --> prefs
```

## Unified context contract

`GET /context/bundle` and `context_core.context_bundle()` return **schema v1** (`context_platform.CONTEXT_BUNDLE_SCHEMA_VERSION`):

- `graph` — scaffold + spine neighborhood
- `open_candidates` / `high_confidence_candidates`
- `recent_evidence` — fused screen/ambient (vault-local detail)
- `related_memory` — corpus prefetch when `subject` set
- `preferences` — active preference claims + clusters (summary)
- `privacy_scope` — reader id + allowed strata for this response
- `connector_intents` — open connector build tasks/candidates
- `resource_poll` — assumed data sources from onboarding (Gmail, ChatGPT, etc.)

MCP callers should pass `for_mcp=true` (default) so related_memory and evidence respect consent strata. Desktop uses `for_mcp=false` for the full local vault view.

## Connector discovery

Minion **assumes data resources** and asks one question at a time:

- Gmail, ChatGPT export, Claude export, Slack, local folders, Calendar

Answers persist in `<data_dir>/onboarding/resource_poll.json` and spawn:

- `graph_candidates` (`connector_intent`) for buildable imports
- `tasks` (`origin=connector_intent`) for human-visible follow-up

See `connector_intent.py` and `POST /onboarding/resource-poll`, `POST /onboarding/connector-intent`.

## Preference promotion

Explicit user answers and council decisions promote to **durable preference claims** before weak ambient inference:

| Source | Action |
| ------ | ------ |
| Onboarding name / connector text | `preference_promotion.record_explicit_preference` (proposed) |
| Council approve/reject | `record_council_feedback` |
| Graph-fill answer on preference gap | `record_graph_answer` |

Claims use `identity.py` (`kind=preference`). Graph `preference` nodes link when confidence ≥ threshold.

## Privacy strata

Defined in [PRIVACY_MATRIX.md](PRIVACY_MATRIX.md) and enforced in `consent_policy.py`:

| Stratum | Examples | Default MCP |
| ------- | -------- | ------------- |
| `raw_evidence` | Ambient chunks, screen-memory paths | Deny |
| `summaries` | Hourly ambient-summary chunks | Deny |
| `graph_facts` | Scaffold nodes, mined relationships | Allow (projections) |
| `preferences` | Active identity preference claims | Allow |
| `projections` | `context_md`, spine, candidate titles | Allow |

## Code index

| Module | Purpose |
| ------ | ------- |
| `context_platform.py` | Schema version, strata labels, bundle enrichment |
| `context_core.py` | `context_bundle()` implementation |
| `preference_promotion.py` | Claim promotion rules |
| `connector_intent.py` | Resource polls + intent persistence |
| `consent_policy.py` | Reader scopes + MCP filter |
| `onboarding_chat.py` | Gemini onboarding dialogue |

## Non-goals (this doc)

- OAuth connector implementations (intents only)
- Replacing SQLite / sqlite-vec
- Exposing raw ambient to MCP by default
