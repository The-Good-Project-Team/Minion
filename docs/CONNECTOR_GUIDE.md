# AI Assistant Connector Extension Guide

This guide explains how to add support for new AI assistants (Perplexity, Copilot, etc.) to Minion's MCP connector system.

## Overview

Minion uses a standardized connector abstraction to integrate with AI assistants via their MCP (Model Context Protocol) configuration systems. Each connector implements the `Connector` base class, providing a consistent interface for:

- Checking if the assistant is installed
- Locating its MCP config file
- Detecting if Minion is already configured
- Connecting Minion by upserting the MCP entry

## Connector Interface

### Base Class

All connectors inherit from `Connector` in `src/connector_base.py`:

```python
from connector_base import Connector

class MyAssistantConnector(Connector):
    @property
    def connector_id(self) -> str:
        """Unique identifier (e.g., 'my-assistant')."""
        return "my-assistant"

    @property
    def display_name(self) -> str:
        """Human-readable name for UI."""
        return "My Assistant"

    @property
    def download_url(self) -> str:
        """URL where users can download this assistant."""
        return "https://example.com/download"

    def get_config_path(self, config_path_override: Optional[str] = None) -> Optional[Path]:
        """Return the MCP config path for this assistant."""
        # Check for override
        if config_path_override:
            return Path(config_path_override).expanduser().resolve()

        # Check environment variable
        env = os.environ.get("MY_ASSISTANT_MCP_CONFIG")
        if env:
            return Path(env).expanduser().resolve()

        # Platform-specific default paths
        if sys.platform == "darwin":
            return Path.home() / ".config" / "my-assistant" / "mcp.json"
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            return Path(appdata) / "MyAssistant" / "mcp.json" if appdata else None
        return Path.home() / ".config" / "my-assistant" / "mcp.json"

    def is_installed(self) -> bool:
        """Check if the assistant app is installed."""
        # Check for skip flag (useful for testing)
        if os.environ.get("MINION_SKIP_MY_ASSISTANT_APP_CHECK"):
            return True

        # Platform-specific installation detection
        if sys.platform == "darwin":
            return any(
                p.is_dir()
                for p in (
                    Path("/Applications/MyAssistant.app"),
                    Path.home() / "Applications" / "MyAssistant.app",
                )
            )
        if sys.platform == "win32":
            # Check common Windows installation paths
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                exe = Path(local) / "MyAssistant" / "assistant.exe"
                if exe.is_file():
                    return True
            pf = os.environ.get("ProgramFiles", "")
            if pf:
                exe = Path(pf) / "MyAssistant" / "assistant.exe"
                if exe.is_file():
                    return True
            return False
        return False
```

### Provided Methods

The base class provides these methods automatically:

- `is_configured(config_path, server_name)` - Check if Minion is configured
- `get_status(server_name)` - Get connection status dict
- `connect(server_name, config_path_override, create_if_missing)` - Connect to the assistant
- `_upsert_mcp_entry(cfg_path, server_name, create_if_missing)` - Idempotent MCP entry upsert
- `_build_mcp_entry()` - Build the Minion MCP server entry
- `_mcp_build_sha()` - Generate build hash for cache invalidation

## Registration

After implementing your connector, register it in the initialization function:

```python
# In src/connector_base.py

def initialize_connectors() -> None:
    """Register all available AI assistant connectors."""
    from connectors import ClaudeDesktopConnector, CursorConnector, MyAssistantConnector

    ConnectorRegistry.register(ClaudeDesktopConnector())
    ConnectorRegistry.register(CursorConnector())
    ConnectorRegistry.register(MyAssistantConnector())  # Add your connector here
    log.info("Initialized %d connectors", len(ConnectorRegistry.list_all()))
```

Also update the imports in `src/connectors/__init__.py`:

```python
"""AI assistant connector implementations."""
from .claude_desktop import ClaudeDesktopConnector
from .cursor import CursorConnector
from .my_assistant import MyAssistantConnector  # Add your connector here

__all__ = ["ClaudeDesktopConnector", "CursorConnector", "MyAssistantConnector"]
```

## API Endpoints

Once registered, your connector is automatically available via the generic API endpoints:

- `GET /connectors` - List all connectors with status
- `GET /connectors/{connector_id}/status` - Get specific connector status
- `POST /connectors/{connector_id}/connect` - Connect to a specific assistant

Example usage:

```bash
# List all connectors
curl http://localhost:8080/connectors

# Get status for your connector
curl http://localhost:8080/connectors/my-assistant/status

# Connect to your assistant
curl -X POST http://localhost:8080/connectors/my-assistant/connect \
  -H "Content-Type: application/json" \
  -d '{"server_name": "minion"}'
```

## Testing

Add tests for your connector in `tests/test_connector_base.py`:

```python
def test_my_assistant_connector():
    """Test MyAssistant connector implementation."""
    from connectors.my_assistant import MyAssistantConnector

    conn = MyAssistantConnector()

    # Test properties
    assert conn.connector_id == "my-assistant"
    assert conn.display_name == "My Assistant"
    assert conn.download_url == "https://example.com/download"

    # Test config path resolution
    cfg_path = conn.get_config_path()
    assert cfg_path is not None

    # Test installation detection (may need mocking)
    installed = conn.is_installed()
    # Assert based on your test environment
```

## MCP Config Format

Most AI assistants use a similar MCP config format:

```json
{
  "mcpServers": {
    "minion": {
      "command": "/path/to/python",
      "args": ["/path/to/mcp_server.py"],
      "env": {
        "MINION_DATA_DIR": "/path/to/minion/data",
        "MINION_BUILD_SHA": "abc123..."
      }
    }
  }
}
```

The base class's `_build_mcp_entry()` method generates this format automatically. If your assistant uses a different format, override the method.

## Environment Variables

Support these environment variables for flexibility:

- `{ASSISTANT}_MCP_CONFIG` - Override config path (e.g., `MY_ASSISTANT_MCP_CONFIG`)
- `MINION_SKIP_{ASSISTANT}_APP_CHECK` - Skip installation detection (useful for testing)

## Examples

See existing implementations:

- `src/connectors/claude_desktop.py` - Claude Desktop connector
- `src/connectors/cursor.py` - Cursor connector

## Best Practices

1. **Platform Support**: Handle macOS, Windows, and Linux where applicable
2. **Config Discovery**: Check environment variables first, then platform-specific defaults
3. **Installation Detection**: Use skip flags for testing environments
4. **Error Handling**: Return `None` for unresolvable paths rather than raising exceptions
5. **Documentation**: Document any assistant-specific quirks in the connector docstring

## Troubleshooting

### Connector not appearing in `/connectors`

- Ensure the connector is registered in `initialize_connectors()`
- Check that the import is added to `connectors/__init__.py`
- Restart the Minion API server

### Config path not found

- Verify the platform-specific path logic
- Check if the environment variable name is correct
- Test with the `config_path_override` parameter

### Installation detection failing

- Verify the app path logic matches actual installation locations
- Use the skip flag for testing: `export MINION_SKIP_MY_ASSISTANT_APP_CHECK=1`
- Check if the assistant uses a different installation mechanism

## Future Enhancements

Potential improvements to the connector system:

- Auto-discovery of connector config paths
- Connector health monitoring
- Automatic reconnection on config changes
- Connector-specific configuration options
