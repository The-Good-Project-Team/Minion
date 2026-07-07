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
- **Completed:** storage_tier column in chunks table with default 'hot'
- **Completed:** Tier promotion infrastructure (hot→warm→cold validation)
- **Completed:** POST /maintenance/storage-tier-promote-stale endpoint
- **Completed:** Storage tier counts and reporting
- **Completed:** Warm tier - chunk summarization/consolidation via LLM
- **Completed:** POST /maintenance/storage-tier-consolidate-warm endpoint
- **Completed:** Cold tier - sparse file storage (gzip-compressed JSON)
- **Completed:** POST /maintenance/storage-tier-offload-cold endpoint
- **Completed:** Lazy hydration when search hits cold chunks
- **Completed:** Search functions (semantic + keyword) auto-hydrate cold chunks

### 3. Background Compaction Jobs
- **Completed:** Ambient consolidation already exists in `ambient_consolidation.py`
- **Completed:** Chunk deduplication via content fingerprint (first 400 chars, SHA-1)
- **Completed:** POST /maintenance/chunk-deduplicate endpoint
- **Completed:** SQLite VACUUM endpoint for space reclamation
- **Completed:** POST /maintenance/vacuum endpoint
- **Completed:** POST /maintenance/run-compaction (runs ambient + dedup together)
- **Note:** Background task scheduling already exists in `memory_lifecycle.py`

### 4. Consent Policy UI
- **Completed:** Visual editor for privacy scopes (desktop vs MCP vs LAN)
- **Completed:** Per-stratum toggles (raw_evidence, summaries, graph_facts, etc.)
- **Completed:** Real-time preview of what each reader can see
- **Completed:** New Settings tab in desktop app with consent policy editor
- **Completed:** Max release level controls per reader (0-5)
- **Completed:** Toggle buttons for each privacy stratum per reader
- **Completed:** Real-time summary showing current access for each reader

### 5. Belief Plasticity with Supersession
- **Completed:** Added `superseded_at` column to identity_claims table with migration
- **Completed:** Updated supersession logic to set superseded_at timestamp when superseded_by changes
- **Completed:** Added GET /identity/history endpoint for revision history with supersession tracking
- **Completed:** Added POST /identity/revert endpoint to revert identity claims to previous versions
- **Completed:** Added TypeScript API client functions (fetchIdentityHistory, revertIdentityClaim)
- **Completed:** Updated IdentityClaim type with superseded_at field
- **Note:** Desktop UI integration can be added later using the new API endpoints

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
