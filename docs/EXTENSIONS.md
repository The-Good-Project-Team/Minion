# Minion Extension Documentation

This guide explains how to extend Minion with custom MCP tools, bounded readers, and consent scopes.

## Overview

Minion's extension model is built around three main extension points:

1. **MCP Tools** - Add new tools that AI assistants can call to interact with your vault
2. **Bounded Readers** - Define custom privacy scopes for different data consumers
3. **Consent Scopes** - Control what data each reader can access

## MCP Tools

MCP (Model Context Protocol) tools are the primary way to extend Minion's capabilities. Tools are defined in `chatgpt_mcp_memory/src/mcp_server.py`.

### Adding a New Tool

To add a new MCP tool:

1. **Implement the tool function** in `mcp_server.py`:

```python
def _tool_my_custom_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """One-line description of what this tool does."""
    conn = _get_conn()
    
    # Extract arguments
    param1 = str(arguments.get("param1") or "").strip()
    param2 = int(arguments.get("param2") or 10)
    
    # Your tool logic here
    result = do_something(conn, param1, param2)
    
    return {"status": "ok", "result": result}
```

2. **Add the tool schema** to the `TOOLS` list:

```python
TOOLS: List[Dict[str, Any]] = [
    # ... existing tools ...
    {
        "name": "my_custom_tool",
        "title": "Human-readable title",
        "description": "Longer description of what this tool does",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "param1": {"type": "string", "description": "Description of param1"},
                "param2": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            },
        },
    },
]
```

3. **Register the tool** in the `_DISPATCH` dictionary:

```python
_DISPATCH = {
    # ... existing tools ...
    "my_custom_tool": _tool_my_custom_tool,
}
```

### Tool Function Guidelines

- **Always get a connection**: Use `conn = _get_conn()` to access the database
- **Validate arguments**: Type-check and sanitize all inputs
- **Return structured responses**: Use `{"status": "ok", ...}` for success, `{"status": "error", "error": "..."}` for failures
- **Handle consent**: Check consent policy before returning sensitive data
- **Be idempotent**: Tools should be safe to call multiple times

### Example: Custom Search Tool

```python
def _tool_search_by_tag(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Search chunks by custom tag metadata."""
    conn = _get_conn()
    tag = str(arguments.get("tag") or "").strip()
    limit = int(arguments.get("limit") or 10)
    
    if not tag:
        return {"status": "error", "error": "tag is required"}
    
    rows = conn.execute(
        "SELECT chunk_id, text, meta FROM chunks WHERE json_extract(meta, '$.tags') LIKE ? LIMIT ?",
        (f"%{tag}%", limit)
    ).fetchall()
    
    return {
        "status": "ok",
        "chunks": [{"chunk_id": r["chunk_id"], "text": r["text"], "meta": r["meta"]} for r in rows],
        "count": len(rows)
    }
```

### Consent-Aware Tools

Tools that return sensitive data should respect the consent policy:

```python
def _tool_sensitive_data_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Example of a consent-aware tool."""
    from consent_policy import hit_allowed_for_mcp, load_policy
    
    conn = _get_conn()
    data_dir = _data_dir()
    
    # Get raw data
    raw_hits = get_sensitive_data(conn)
    
    # Filter by consent policy
    policy = load_policy(data_dir)
    filtered_hits = [h for h in raw_hits if hit_allowed_for_mcp(h, policy)]
    
    return {"status": "ok", "hits": filtered_hits}
```

## Bounded Readers

Bounded readers define custom privacy scopes for different data consumers. Readers are configured in `chatgpt_mcp_memory/src/consent_policy.py`.

### Reader Types

Minion includes several built-in readers:

- **`local_ui`** - The desktop app UI (sees everything by default)
- **`mcp`** - AI assistants via MCP (restricted to summaries and graph facts)
- **`connector_builder`** - External connector construction tools
- **`export_bundle`** - Data export bundles

### Adding a Custom Reader

To add a new reader type:

1. **Define the reader's default strata** in `READER_STRATA_DEFAULTS`:

```python
READER_STRATA_DEFAULTS: Dict[str, List[str]] = {
    # ... existing readers ...
    "my_custom_reader": [
        STRATUM_SUMMARIES,
        STRATUM_GRAPH_FACTS,
        STRATUM_WORK_CONTEXT,
    ],
}
```

2. **Configure the reader in `DEFAULT_POLICY`**:

```python
DEFAULT_POLICY: Dict[str, Any] = {
    "schema_version": 1,
    "readers": {
        # ... existing readers ...
        "my_custom_reader": {
            "allowed_strata": list(READER_STRATA_DEFAULTS["my_custom_reader"]),
            "max_release_level": 3,
            "deny_chunk_source_kinds": ["ambient"],
            "deny_path_substrings": ["/private/"],
        },
    },
}
```

3. **Use the reader in your code**:

```python
from consent_policy import hit_allowed_for_reader, load_policy

def filter_for_custom_reader(hits: List[Hit], data_dir: Path) -> List[Hit]:
    policy = load_policy(data_dir)
    return [h for h in hits if hit_allowed_for_reader(h, policy, "my_custom_reader")]
```

### Privacy Strata

Privacy strata define categories of data sensitivity:

| Stratum | Description | Examples |
| ------- | ----------- | -------- |
| `raw_evidence` | Full unprocessed data | Ambient chunks, screen captures |
| `summaries` | Processed summaries | Rolled-up ambient summaries |
| `graph_facts` | Knowledge graph nodes | Person entities, project relationships |
| `work_context` | Releasable work context | Level 3 fused screen events |
| `preferences` | User preferences | Identity preference claims |
| `projections` | Composed context bundles | Multi-source fused insights |

### Release Levels

Release levels (0-5) control data sensitivity:

- **0**: No personal/context data
- **1**: Generic state
- **2**: Broad project category
- **3**: Specific releasable work context
- **4**: Sensitive operational detail
- **5**: Raw private evidence

## Consent Scopes

Consent scopes define fine-grained access control for readers.

### Reader Configuration Options

Each reader in the consent policy supports these options:

```python
{
    "allowed_strata": ["summaries", "graph_facts"],  # Which privacy strata to allow
    "max_release_level": 3,                          # Maximum release level (0-5)
    "release_without_ok_level": 2,                   # Max level without explicit approval
    "release_notice_threshold": 3,                   # When to show release notices
    "deny_chunk_source_kinds": ["ambient"],          # Block specific chunk kinds
    "deny_path_substrings": ["/private/"],           # Block paths matching substrings
    "releasable_chunk_kinds": ["graph-fact"],        # Kinds that can be released
    "allow_screen_context_tools": True,              # Enable screen context tools
}
```

### Example: Strict Reader

A reader that only sees graph facts and work context:

```python
"strict_reader": {
    "allowed_strata": [STRATUM_GRAPH_FACTS, STRATUM_WORK_CONTEXT],
    "max_release_level": 2,
    "deny_chunk_source_kinds": ["ambient", "ambient-ax"],
    "deny_path_substrings": ["/private/", "/confidential/"],
}
```

### Example: Permissive Reader

A reader that can see almost everything:

```python
"permissive_reader": {
    "allowed_strata": [
        STRATUM_RAW_EVIDENCE,
        STRATUM_SUMMARIES,
        STRATUM_GRAPH_FACTS,
        STRATUM_WORK_CONTEXT,
        STRATUM_PREFERENCES,
    ],
    "max_release_level": 5,
}
```

## API Reference

### Core Functions

#### `consent_policy.load_policy(data_dir: Path) -> Dict[str, Any]`

Load the consent policy from disk, merging with defaults.

```python
from consent_policy import load_policy

policy = load_policy(Path("~/Library/Application Support/Minion 2/data"))
```

#### `consent_policy.hit_allowed_for_reader(hit: Hit, policy: Dict[str, Any], reader_id: str, **kwargs) -> bool`

Check if a hit is allowed for a specific reader.

```python
from consent_policy import hit_allowed_for_reader

allowed = hit_allowed_for_reader(hit, policy, "mcp")
```

#### `consent_policy.filter_hits_for_mcp(hits: List[Hit], data_dir: Path, **kwargs) -> List[Hit]`

Filter a list of hits for MCP consumption.

```python
from consent_policy import filter_hits_for_mcp

filtered = filter_hits_for_mcp(hits, data_dir)
```

### MCP Server Functions

#### `_get_conn() -> sqlite3.Connection`

Get a thread-local database connection.

```python
from mcp_server import _get_conn

conn = _get_conn()
```

#### `_data_dir() -> Path`

Get the current data directory path.

```python
from mcp_server import _data_dir

data_dir = _data_dir()
```

### Store Functions

#### `store.search(conn, query: str, top_k: int = 8, **kwargs) -> List[Hit]`

Semantic search over the corpus.

```python
from store import search

hits = search(conn, "my query", top_k=10)
```

#### `store.keyword_search(conn, query: str, top_k: int = 8, **kwargs) -> List[Hit]`

Keyword search using FTS5.

```python
from store import keyword_search

hits = keyword_search(conn, "my query", top_k=10)
```

## Testing Extensions

### Unit Tests

Add unit tests in `chatgpt_mcp_memory/tests/`:

```python
# tests/test_my_extension.py
import pytest
from mcp_server import _tool_my_custom_tool

def test_my_custom_tool():
    result = _tool_my_custom_tool({"param1": "test", "param2": 5})
    assert result["status"] == "ok"
    assert "result" in result
```

### Integration Tests

Test tools through the MCP interface:

```python
def test_tool_via_mcp():
    from mcp_server import handle_jsonrpc
    
    req = {
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "my_custom_tool",
            "arguments": {"param1": "test", "param2": 5}
        }
    }
    
    response = handle_jsonrpc(req)
    assert response["result"]["status"] == "ok"
```

## Best Practices

1. **Privacy First**: Always consider what data your tool exposes and apply appropriate consent filtering
2. **Idempotent Operations**: Tools should be safe to call multiple times without side effects
3. **Clear Error Messages**: Return descriptive error messages to help users debug issues
4. **Performance**: Use database indexes and limit result sets to avoid timeouts
5. **Documentation**: Document your tools with clear descriptions and parameter explanations
6. **Testing**: Write both unit and integration tests for your extensions
7. **Backward Compatibility**: Avoid breaking changes to existing tool interfaces

## Examples

### Example 1: Custom Analytics Tool

```python
def _tool_analyze_reading_patterns(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze the user's document reading patterns."""
    conn = _get_conn()
    days = int(arguments.get("days") or 30)
    
    import time as _time
    since = _time.time() - days * 86400
    
    rows = conn.execute(
        """SELECT kind, COUNT(*) as count 
           FROM chunks 
           WHERE updated_at >= ? 
           GROUP BY kind""",
        (since,)
    ).fetchall()
    
    return {
        "status": "ok",
        "patterns": {r["kind"]: r["count"] for r in rows},
        "period_days": days
    }
```

### Example 2: Custom Reader for External API

```python
# In consent_policy.py
READER_STRATA_DEFAULTS["external_api"] = [
    STRATUM_SUMMARIES,
    STRATUM_GRAPH_FACTS,
]

DEFAULT_POLICY["readers"]["external_api"] = {
    "allowed_strata": list(READER_STRATA_DEFAULTS["external_api"]),
    "max_release_level": 2,
    "deny_chunk_source_kinds": ["ambient", "screen-event"],
}

# Usage
from consent_policy import hit_allowed_for_reader, load_policy

def prepare_external_api_response(hits: List[Hit], data_dir: Path) -> List[Hit]:
    policy = load_policy(data_dir)
    return [h for h in hits if hit_allowed_for_reader(h, policy, "external_api")]
```

## Troubleshooting

### Tool Not Appearing in MCP

- Check that the tool is in the `TOOLS` list
- Verify the tool is registered in `_DISPATCH`
- Ensure the tool function signature matches the expected pattern

### Consent Policy Not Applied

- Verify the reader ID matches exactly
- Check that the policy file exists in the data directory
- Ensure `load_policy()` is called with the correct data_dir path

### Database Connection Issues

- Always use `_get_conn()` to get connections
- Don't cache connections across threads
- Close connections only if you created them manually

## Resources

- **MCP Server**: `chatgpt_mcp_memory/src/mcp_server.py`
- **Consent Policy**: `chatgpt_mcp_memory/src/consent_policy.py`
- **Privacy Matrix**: `docs/PRIVACY_MATRIX.md`
- **Store API**: `chatgpt_mcp_memory/src/store.py`
- **Testing**: `chatgpt_mcp_memory/tests/`
