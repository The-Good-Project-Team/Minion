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
- **Completed:** Collapsible sections by event type (ingest, ambient, graph, errors)
- **Completed:** Filter by event type and time range
- **Completed:** Click to jump to related source or graph node
- **Completed:** Activity feed display component in desktop app
- **Completed:** Integration with existing /feed API endpoint

### 7. Interactive Life Graph Visualization
- **Completed:** Force-directed graph with node/edge filtering
- **Completed:** Color-coding by node type (person, project, obligation)
- **Completed:** Click to view evidence and edit relationships
- **Completed:** 3D graph visualization using react-force-graph-3d
- **Completed:** Graph tab in desktop app with filtering controls
- **Completed:** Node details panel showing relationships and evidence
- **Note:** Requires knowledge graph to be built first via "Build graph" action to display nodes

### 8. Audit Log Viewer
- **Completed:** Log all identity and graph changes with timestamps
- **Completed:** Show who/what made the change (user vs model)
- **Completed:** Allow rollback of specific changes
- **Completed:** Added graph_audit_log table for graph changes
- **Completed:** Added unified audit log API endpoint (GET /audit)
- **Completed:** Added rollback API endpoint (POST /audit/{id}/rollback)
- **Completed:** Audit log viewer UI in Settings with filtering
- **Completed:** Rollback button for identity claim entries

### 9. Identity Summary Builder
- **Completed:** Enhanced build_identity_summary to extract motifs from ambient and corpus data
- **Completed:** Generate narrative summaries with evidence links
- **Completed:** Show current stance vs historical changes with evolution tracking
- **Completed:** Added include_evidence parameter to control evidence display
- **Completed:** Updated API endpoint (GET /identity/summary) with include_evidence parameter
- **Completed:** Created IdentityMirror UI component with markdown rendering
- **Completed:** Added IdentityMirror to home tab in desktop app

### 10. Multi-Monitor Screen Capture
- **Completed:** Detect all connected displays using CGGetActiveDisplayList
- **Completed:** Associate windows with their display based on window center position
- **Completed:** Allow per-monitor capture toggles via environment variables (screen_reader_display_{display_id})
- **Completed:** Support deny list file (display_deny.txt) for blocking specific displays
- **Completed:** Include display_id in window snapshot records
- **Implementation:** Extended `screen_reader.rs` with display enumeration and filtering

### 11. Local Whisper Audio Transcription
- **Completed:** Integrate faster-whisper for local audio transcription
- **Completed:** Add transcription to ambient-audio chunks via listening_ingest.py
- **Completed:** Configurable quality/speed tradeoff via MINION_WHISPER_MODEL environment variable (default: tiny.en)
- **Completed:** CPU int8 compute type for efficiency
- **Completed:** Integration with audio/video parsers and ambient pipeline
- **Implementation:** Already exists in parsers/audio.py and listening_ingest.py

### 12. Enhanced MCP Tools
- **Completed:** `working_context` - current active sources and recent attention (already existed in second_brain.py)
- **Completed:** `wiki_proposal` - suggest wiki pages from graph nodes without pages or recent activity patterns
- **Completed:** Better error handling and documentation throughout MCP server
- **Implementation:** Extended `mcp_server.py` with wiki_proposal tool

## Low Priority

### 13. Extension Documentation
- **Completed:** Developer guide for building MCP extensions
- **Completed:** Examples of bounded readers and consent scopes
- **Completed:** API reference for extension points
- **Completed:** Tool implementation guidelines with code examples
- **Completed:** Consent-aware tool patterns
- **Completed:** Testing guidelines for extensions
- **Implementation:** Created `docs/EXTENSIONS.md`

### 14. Visual Indicators for Export Sources
- **Completed:** Badge or icon for ChatGPT export sources in the UI
- **Completed:** Purple "AI Export" badge for export sources
- **Completed:** Display export metadata (conversation count, unique IDs)
- **Completed:** Separate section for "AI Chat Exports" vs regular files (via existing source_type filter)
- **Implementation:** Added badge and metadata display in App.tsx sources list

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
