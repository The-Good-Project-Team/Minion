# Export Scheduler Guide

The export scheduler monitors a designated folder for new AI assistant export files (ChatGPT, Claude, Copilot, etc.) and automatically ingests them into Minion.

## Overview

Since ChatGPT and other AI assistants don't provide automated export APIs, users must manually download their conversation exports. The export scheduler simplifies this by:

- **Automatic monitoring**: Checks a watch folder at configurable intervals (default: 1 hour)
- **Deduplication**: Skips already-ingested exports using content hashing
- **Manual trigger**: Allows on-demand ingestion of specific files
- **Configuration**: Customizable watch path and check intervals via settings or environment variables

## Configuration

### Environment Variables

- `MINION_EXPORT_WATCH_PATH`: Path to folder containing export files
- `MINION_EXPORT_INTERVAL_SEC`: Check interval in seconds (minimum: 300)
- `MINION_DISABLE_EXPORT_SCHEDULER`: Set to "1", "true", or "yes" to disable

### Settings File

Add to your Minion settings file:

```json
{
  "export_watch_path": "/path/to/exports",
  "export_interval_sec": 3600
}
```

### Default Behavior

If no configuration is provided:
- Watch path: `{data_dir}/inbox/exports`
- Check interval: 3600 seconds (1 hour)

## API Endpoints

### Get Export Scheduler Status

```bash
GET /exports/status
```

Returns:
```json
{
  "enabled": true,
  "watch_path": "/path/to/exports",
  "watch_path_exists": true,
  "interval_sec": 3600,
  "interval_hours": 1.0
}
```

### Trigger Manual Export Ingestion

```bash
POST /exports/trigger
Content-Type: application/json

{
  "export_path": "/path/to/export.json"  // Optional
}
```

Without `export_path`: Checks watch folder for new exports.
With `export_path`: Ingests the specific file.

Returns:
```json
{
  "status": "completed",
  "watch_path": "/path/to/exports",
  "total": 2,
  "successful": 2,
  "failed": 0,
  "results": [
    {
      "path": "/path/to/export1.json",
      "success": true,
      "source_id": "source-123",
      "chunks": 42
    }
  ]
}
```

### Configure Export Scheduler

```bash
POST /exports/config
Content-Type: application/json

{
  "watch_path": "/new/path",
  "interval_sec": 1800,
  "enabled": true
}
```

## Export File Detection

The scheduler detects export files by:

1. **File extension**: `.json` or `.zip`
2. **Filename patterns**: Contains keywords like "chatgpt", "claude", "copilot", "gemini", "export", "conversations"

## Deduplication

To avoid re-ingesting the same export:

1. Computes a content hash (SHA-256 truncated to 32 chars) based on:
   - First 1MB of file content
   - File size
   - File modification time
2. Compares against hashes of already-ingested sources
3. Skips files with matching hashes

## Usage Examples

### Basic Setup

1. Create a watch folder:
```bash
mkdir -p ~/Library/Application\ Support/Minion/inbox/exports
```

2. Download your ChatGPT export to that folder

3. The scheduler will automatically ingest it on the next check (within 1 hour)

### Manual Trigger

```bash
# Ingest all new exports in watch folder
curl -X POST http://localhost:8765/exports/trigger

# Ingest a specific file
curl -X POST http://localhost:8765/exports/trigger \
  -H "Content-Type: application/json" \
  -d '{"export_path": "/path/to/chatgpt_export.json"}'
```

### Custom Watch Path

```bash
# Set via API
curl -X POST http://localhost:8765/exports/config \
  -H "Content-Type: application/json" \
  -d '{"watch_path": "/custom/export/folder"}'

# Or set via environment variable
export MINION_EXPORT_WATCH_PATH="/custom/export/folder"
```

### Change Check Interval

```bash
# Check every 30 minutes
curl -X POST http://localhost:8765/exports/config \
  -H "Content-Type: application/json" \
  -d '{"interval_sec": 1800}'

# Or set via environment variable
export MINION_EXPORT_INTERVAL_SEC=1800
```

### Disable Scheduler

```bash
# Disable via API
curl -X POST http://localhost:8765/exports/config \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Or set via environment variable
export MINION_DISABLE_EXPORT_SCHEDULER=1
```

## Troubleshooting

### Exports not being ingested

1. Check scheduler status:
```bash
curl http://localhost:8765/exports/status
```

2. Verify watch path exists and contains export files

3. Check if scheduler is disabled

4. Review logs for errors

### File not recognized as export

Ensure the file:
- Has `.json` or `.zip` extension
- OR contains export keywords in filename
- Is a regular file (not a directory)

### Export re-ingested after changes

The content hash includes modification time, so editing a file will trigger re-ingestion. To avoid this:
- Don't modify export files after ingestion
- Or move processed files to a different location

## Limitations

- ChatGPT and other assistants don't provide automated export APIs
- Users must manually download exports to the watch folder
- No real-time notifications when new exports are available
- Large exports may take time to ingest (progress tracking not yet implemented)

## Future Enhancements

Potential improvements:

- Progress indicators for large exports
- Automatic export download from assistant APIs (if available)
- Export format validation before ingestion
- Webhook notifications for export ingestion events
- Export-specific search filters in the UI
