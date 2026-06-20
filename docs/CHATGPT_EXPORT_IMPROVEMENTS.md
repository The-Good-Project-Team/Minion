# ChatGPT Export Ingestion Improvements

## Goal
Improve the ChatGPT export ingestion experience to make it the primary workflow for helping users port their ChatGPT history to other AI assistants (Claude, Cursor, etc.).

## Current State
- ChatGPT exports can be dropped into Minion
- Basic parsing exists but lacks robustness
- No progress indicators for large exports
- Limited error handling for malformed exports
- No deduplication for re-added exports

## Proposed Improvements

### 1. Better Error Handling for Malformed Exports

**Problem**: Users may have corrupted or incomplete exports that fail silently or with unclear errors.

**Solution**:
- Add validation before ingestion (check for expected structure)
- Provide clear error messages with file path and line number
- Show which files failed vs succeeded in batch exports
- Add "retry failed files" option
- Log detailed parsing errors to Activity feed

**Implementation**:
- Extend `chatgpt_mcp_memory/src/parsers/chatgpt_export.py`
- Add validation function `validate_export_structure()`
- Update ingest endpoint to return per-file status
- Add error UI in desktop showing failed files with retry button

### 2. Support for More Export Formats

**Problem**: ChatGPT exports come in different structures (JSON, different folder layouts).

**Solution**:
- Detect export format automatically (JSON vs markdown vs custom)
- Support both single-file JSON exports and folder-based exports
- Handle different ChatGPT export versions
- Add format detection in Activity feed

**Implementation**:
- Add format detection in `chatgpt_mcp_memory/src/parsers/`
- Create format-specific parsers
- Add metadata to sources indicating export format
- UI shows detected format

### 3. Progress Indicator for Large Exports

**Problem**: Large exports (thousands of conversations) take time to ingest with no feedback.

**Solution**:
- Show real-time progress (X/Y conversations processed)
- Show estimated time remaining
- Allow cancellation mid-ingest
- Persist progress so interrupted exports can be resumed

**Implementation**:
- Add progress tracking to ingest pipeline
- WebSocket events for progress updates
- UI progress bar with cancel button
- Add `ingest_progress` table to track state

### 4. Deduplication for Re-Added Exports

**Problem**: Users may add the same export multiple times, creating duplicate sources.

**Solution**:
- Detect duplicate exports by content hash or export metadata
- Skip already-ingested conversations
- Offer to "refresh" export (only add new conversations)
- Show "X conversations skipped (duplicate)" message

**Implementation**:
- Add `export_id` or `content_hash` to sources
- Check for existing sources before ingest
- Add "refresh export" mode (incremental update)
- UI shows dedup stats

### 5. Better Chunking for Chat Threads

**Problem**: Chat conversations should preserve context across messages.

**Solution**:
- Chunk by conversation thread rather than individual messages
- Preserve conversation metadata (title, date, participants)
- Add cross-reference chunks (conversation summary + individual messages)
- Better semantic search within conversations

**Implementation**:
- Update chunking strategy in `chatgpt_mcp_memory/src/ingest.py`
- Add conversation-level chunks
- Add message-level chunks with conversation context
- Update retrieval to prioritize conversation-level chunks

### 6. Export-Specific Search

**Problem**: Users want to search only within their ChatGPT exports.

**Solution**:
- Add filter to search only ChatGPT export sources
- Search by conversation title, date range, or participant
- Faceted search: filter by export, date, kind
- Show export metadata in search results

**Implementation**:
- Add `source_kind` filter to `GET /sources` and search endpoints
- Add export metadata to source schema
- UI filter dropdown for "ChatGPT exports only"
- Add date range picker for exports

### 7. Visual Indicators for Exports

**Problem**: Hard to distinguish ChatGPT exports from regular files in the UI.

**Solution**:
- Badge or icon for ChatGPT export sources
- Separate section for "AI Chat Exports" in Sources
- Show export metadata (conversation count, date range)
- Color-code by export source (ChatGPT, Claude, etc.)

**Implementation**:
- Add `source_kind` field to Source type
- Update UI to show badges/icons for exports
- Add "Exports" section in Sources view
- Add export metadata display

### 8. Test Workflow Verification

**Problem**: Hard to verify end-to-end workflow (drop file → Claude retrieves it).

**Solution**:
- Add "Test" button that drops sample file and verifies retrieval
- Show MCP tool usage in Activity feed
- End-to-end test: drop file → ask Claude → verify response
- Success/failure indicator for test

**Implementation**:
- Add test endpoint that creates sample source
- Add "Test Minion" button in UI
- Monitor MCP tool calls and log to Activity
- Show test result with green/red indicator

## Implementation Order

1. **Error Handling** (foundational - makes other changes safer)
2. **Progress Indicator** (user experience - large exports are painful without it)
3. **Deduplication** (data quality - prevents bloat)
4. **Better Chunking** (search quality - core value)
5. **Export-Specific Search** (user workflow - primary use case)
6. **Visual Indicators** (UI clarity - nice to have)
7. **Test Workflow** (verification - builds confidence)
8. **More Export Formats** (compatibility - expand reach)

## Testing Strategy

- Unit tests for each parser format
- Integration tests for ingest pipeline
- E2E test: drop export → search → verify Claude can retrieve
- Performance test with large export (10k+ conversations)
- Error injection tests (malformed files, partial exports)

## Success Metrics

- Export ingest success rate > 95%
- Average ingest time for 1k conversations < 30 seconds
- Duplicate detection rate > 90%
- User-reported "can't find my export" issues < 5%
- End-to-end test pass rate 100%
