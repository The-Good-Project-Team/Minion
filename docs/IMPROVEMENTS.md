# Minion Improvements Roadmap

This document tracks all potential improvements for Minion beyond the core ChatGPT export workflow.

## Completed

- **Cursor MCP Integration** - One-click connect for Cursor editor (similar to Claude Desktop)
  - Added `GET /connect/cursor/status` endpoint
  - Added `POST /connect/cursor` endpoint
  - Added TypeScript API client functions
  - Added UI checklist item for Cursor connect

- **Faceted Search with Time-Based Filtering** - UI filters for sources
  - Added `source_type` filter (file, chat_export, external, ambient) to `GET /sources`
  - Added `time_range` filter (last_hour, last_day, last_week, all) to `GET /sources`
  - Updated TypeScript API client with new filter parameters
  - Added dropdown filter UI in desktop app sources section
  - Backend filters map source_type to kind groups and convert time_range to timestamps

## High Priority

### 2. Tiered Storage (Hot/Warm/Cold)
- Implement storage tiers in `store.py` with `storage_tier` column
- Hot: Recent chunks in SQLite (default)
- Warm: Summarized/consolidated chunks
- Cold: Offloaded to sparse files or secondary volume
- Add lazy hydration when search hits cold IDs
- Implementation: Add `POST /maintenance/storage-tier-promote-stale` job

### 3. Background Compaction Jobs
- Consolidate repetitive ambient captures into summaries
- Deduplicate near-duplicate chunks using fingerprinting
- Vacuum SQLite pages periodically
- Implementation: Add background task queue in `api.py`

### 4. Consent Policy UI
- Visual editor for privacy scopes (desktop vs MCP vs LAN)
- Per-stratum toggles (raw_evidence, summaries, graph_facts, etc.)
- Real-time preview of what each reader can see
- Implementation: New Settings tab, extend `consent_policy.py`

### 5. Belief Plasticity with Supersession
- Add `superseded_by` and `superseded_at` to identity claims
- UI to view revision history and revert changes
- Differentiate user-authored vs model-proposed changes
- Implementation: Extend `identity.py` schema, add history viewer in desktop

## Medium Priority

### 6. Improved Activity Feed
- Collapsible sections by event type (ingest, ambient, graph, errors)
- Filter by event type and time range
- Click to jump to related source or graph node
- Implementation: Extend WebSocket events, add filter UI

### 7. Interactive Life Graph Visualization
- Force-directed graph with node/edge filtering
- Color-coding by node type (person, project, obligation)
- Click to view evidence and edit relationships
- Implementation: Use D3 or similar library in desktop

### 8. Audit Log Viewer
- Log all identity and graph changes with timestamps
- Show who/what made the change (user vs model)
- Allow rollback of specific changes
- Implementation: Add audit table, viewer in Settings

### 9. Identity Summary Builder
- Extract motifs from ambient and corpus data
- Generate narrative summaries with evidence links
- Show current stance vs historical changes
- Implementation: Extend `identity.py`, add Mirror UI

### 10. Multi-Monitor Screen Capture
- Detect all connected displays
- Allow per-monitor capture toggles
- Support different capture settings per monitor
- Implementation: Extend `screen_reader.rs` in Rust

### 11. Local Whisper Audio Transcription
- Integrate local Whisper model for ambient audio
- Add transcription to ambient-audio chunks
- Configurable quality/speed tradeoff
- Implementation: Add to `ambient_pipeline.py`

### 12. Enhanced MCP Tools
- `working_context` - current active sources and recent attention
- `wiki_proposal` - suggest wiki pages from graph
- Better error handling and documentation
- Implementation: Extend `mcp_server.py`

## Low Priority

### 13. Extension Documentation
- Developer guide for building MCP extensions
- Examples of bounded readers and consent scopes
- API reference for extension points
- Implementation: New `docs/EXTENSIONS.md`

### 14. Visual Indicators for Export Sources
- Badge or icon for ChatGPT export sources in the UI
- Separate section for "AI Chat Exports" vs regular files
- Show export metadata (date range, conversation count)
- Implementation: Add `source_kind` field, update UI

### 15. Test Workflow Verification
- Add "Test" button that drops sample file and verifies retrieval
- Show MCP tool usage in Activity feed
- End-to-end test: drop file → ask Claude → verify response
- Implementation: Add test endpoint, UI test button

## Future Considerations

### 16. Additional AI Assistant Connectors
- Perplexity, Copilot, etc. (if there's demand)
- Standardized connector interface
- Implementation: Add connector abstraction layer

### 17. Export Tools
- One-click export from ChatGPT (if API allows)
- Scheduled export sync
- Implementation: Add export scheduler

### 18. Work vs Personal Separation
- Separate vault namespaces or profiles
- Different consent scopes per profile
- Implementation: Add profile system to data model

## Implementation Notes

- Follow the existing code style and patterns
- Add tests for new features (unit + integration)
- Update documentation as features are added
- Consider performance impact for large corpora
- Maintain backward compatibility where possible
