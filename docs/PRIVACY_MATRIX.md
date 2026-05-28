# Privacy matrix — reader scopes

What each **reader** may see. Enforced in `consent_policy.py`; defaults below match `DEFAULT_POLICY`.

## Strata

| Stratum | Description | Typical storage |
| ------- | ----------- | ---------------- |
| `raw_evidence` | Full chunk text from ambient, screen capture, clipboard | `kind=ambient`, paths under `/ambient/`, `/screen-memory/` |
| `summaries` | Rolled-up ambient / attention summaries | `ambient-summary` chunks, `GET /attention/summary` |
| `graph_facts` | Durable graph nodes/edges, scaffold fill | `graph_nodes`, mined writes |
| `preferences` | Identity claims `kind=preference`, preference clusters | `identity_claims`, `preference_clusters` |
| `projections` | Composed markdown bundles without raw captures | `context_bundle.context_md`, graph spine |

## Readers

| Reader | Who | Default strata |
| ------ | --- | -------------- |
| `local_ui` | Minion desktop app on this Mac | All strata (full vault) |
| `mcp` | External agents via MCP (`ask_minion`, etc.) | `graph_facts`, `preferences`, `projections` only |
| `connector_builder` | Minion-generated import scripts / inbox drops | `raw_evidence` for paths the user opted into; no MCP export |
| `export_bundle` | User-initiated zip export | User-selected; identity snapshot + chunk index metadata |

## MCP defaults (deny unless opted in)

- Chunk kinds: `ambient`, `ambient-ax`
- Path substrings: `/screen-memory/`, `/ambient/`
- Screen-context MCP tools: configurable (`allow_screen_context_tools`)

## HTTP search note

`POST /search` on loopback is **unfiltered** so the human sees their full vault locally. MCP and `context_bundle(for_mcp=true)` apply filters.

## Changing policy

- File: `<data_dir>/consent_policy.json`
- API: `GET/PUT /settings/consent`
- Programmatic: `consent_policy.reader_allowed_strata(reader_id)`

## Product rule

> External agents get **projections**, not surveillance. Local UI gets **truth** with receipts.
