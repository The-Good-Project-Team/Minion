"""Tests for connector abstraction layer."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from connector_base import Connector, ConnectorRegistry


class MockConnector(Connector):
    """Mock connector for testing."""

    def __init__(self, connector_id: str, display_name: str, download_url: str):
        self._id = connector_id
        self._name = display_name
        self._url = download_url
        self._installed = False
        self._config_path = None

    @property
    def connector_id(self) -> str:
        return self._id

    @property
    def display_name(self) -> str:
        return self._name

    @property
    def download_url(self) -> str:
        return self._url

    def get_config_path(self, config_path_override: str = None):
        return self._config_path or config_path_override

    def is_installed(self) -> bool:
        return self._installed

    def set_installed(self, installed: bool):
        self._installed = installed

    def set_config_path(self, path: Path):
        self._config_path = path


def test_connector_registry():
    """Test connector registration and retrieval."""
    # Clear registry before test
    ConnectorRegistry._connectors.clear()
    
    registry = ConnectorRegistry()
    conn = MockConnector("test", "Test Connector", "https://example.com")

    registry.register(conn)

    # Test retrieval
    assert registry.get("test") is conn
    assert registry.get("nonexistent") is None

    # Test list_all
    all_connectors = registry.list_all()
    assert "test" in all_connectors
    assert all_connectors["test"] is conn


def test_connector_status():
    """Test connector status reporting."""
    conn = MockConnector("test", "Test Connector", "https://example.com")
    conn.set_installed(False)

    status = conn.get_status()
    assert status["installed"] is False
    assert status["configured"] is False
    assert status["connected"] is False
    assert status["config_path"] is None

    conn.set_installed(True)
    status = conn.get_status()
    assert status["installed"] is True
    assert status["configured"] is False
    assert status["connected"] is False


def test_connector_is_configured():
    """Test connector configuration detection."""
    conn = MockConnector("test", "Test Connector", "https://example.com")

    # No config path
    assert conn.is_configured() is False

    # Config path doesn't exist
    conn.set_config_path(Path("/nonexistent/config.json"))
    assert conn.is_configured() is False

    # Config exists but no mcpServers
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        cfg_path.write_text(json.dumps({}))
        conn.set_config_path(cfg_path)
        assert conn.is_configured() is False

    # Config exists with mcpServers but no minion entry
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        cfg_path.write_text(json.dumps({"mcpServers": {}}))
        conn.set_config_path(cfg_path)
        assert conn.is_configured() is False

    # Config exists with minion entry
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        cfg_path.write_text(json.dumps({"mcpServers": {"minion": {}}}))
        conn.set_config_path(cfg_path)
        assert conn.is_configured() is True


def test_connector_connect_not_installed():
    """Test connect fails when not installed."""
    conn = MockConnector("test", "Test Connector", "https://example.com")
    conn.set_installed(False)

    with pytest.raises(ValueError, match="not installed"):
        conn.connect()


def test_connector_connect_no_config_path():
    """Test connect fails when config path cannot be resolved."""
    conn = MockConnector("test", "Test Connector", "https://example.com")
    conn.set_installed(True)
    conn.set_config_path(None)

    with pytest.raises(ValueError, match="could not resolve"):
        conn.connect()


def test_connector_upsert_creates_config():
    """Test MCP entry creation when config doesn't exist."""
    conn = MockConnector("test", "Test Connector", "https://example.com")
    conn.set_installed(True)

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        conn.set_config_path(cfg_path)

        # Mock the _build_mcp_entry method to avoid State dependency
        original_build = conn._build_mcp_entry
        conn._build_mcp_entry = lambda: {
            "command": "python",
            "args": ["mcp_server.py"],
            "env": {"MINION_DATA_DIR": str(tmpdir), "MINION_BUILD_SHA": "test123"},
        }

        result = conn._upsert_mcp_entry(cfg_path, "minion", create_if_missing=True)

        assert result["action"] == "created"
        assert result["config_path"] == str(cfg_path)
        assert result["server_name"] == "minion"
        assert result["backup_path"] is None

        # Verify config was created
        assert cfg_path.exists()
        config = json.loads(cfg_path.read_text())
        assert "mcpServers" in config
        assert "minion" in config["mcpServers"]

        # Restore original method
        conn._build_mcp_entry = original_build


def test_connector_upsert_refreshes_config():
    """Test MCP entry refresh when config exists but differs."""
    conn = MockConnector("test", "Test Connector", "https://example.com")
    conn.set_installed(True)

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        conn.set_config_path(cfg_path)

        # Create initial config with different entry
        cfg_path.write_text(json.dumps({"mcpServers": {"minion": {"old": "entry"}}}))

        # Mock the _build_mcp_entry method to avoid State dependency
        original_build = conn._build_mcp_entry
        conn._build_mcp_entry = lambda: {
            "command": "python",
            "args": ["mcp_server.py"],
            "env": {"MINION_DATA_DIR": str(tmpdir), "MINION_BUILD_SHA": "test123"},
        }

        result = conn._upsert_mcp_entry(cfg_path, "minion", create_if_missing=True)

        assert result["action"] == "refreshed"
        assert result["config_path"] == str(cfg_path)
        assert result["backup_path"] is not None

        # Verify backup was created
        backup_path = Path(result["backup_path"])
        assert backup_path.exists()

        # Verify config was updated
        config = json.loads(cfg_path.read_text())
        assert config["mcpServers"]["minion"] != {"old": "entry"}

        # Restore original method
        conn._build_mcp_entry = original_build


def test_connector_upsert_noop():
    """Test MCP entry no-op when config already matches."""
    conn = MockConnector("test", "Test Connector", "https://example.com")
    conn.set_installed(True)

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        conn.set_config_path(cfg_path)

        # Mock the _build_mcp_entry method to avoid State dependency
        original_build = conn._build_mcp_entry
        conn._build_mcp_entry = lambda: {
            "command": "python",
            "args": ["mcp_server.py"],
            "env": {"MINION_DATA_DIR": str(tmpdir), "MINION_BUILD_SHA": "test123"},
        }

        # First create the entry
        conn._upsert_mcp_entry(cfg_path, "minion", create_if_missing=True)

        # Second call should be no-op
        result = conn._upsert_mcp_entry(cfg_path, "minion", create_if_missing=True)

        assert result["action"] == "noop"
        assert result["backup_path"] is None

        # Restore original method
        conn._build_mcp_entry = original_build


def test_connector_upsert_skip_missing():
    """Test MCP entry skip when config doesn't exist and create_if_missing=False."""
    conn = MockConnector("test", "Test Connector", "https://example.com")
    conn.set_installed(True)

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        conn.set_config_path(cfg_path)

        # Mock the _build_mcp_entry method to avoid State dependency
        original_build = conn._build_mcp_entry
        conn._build_mcp_entry = lambda: {
            "command": "python",
            "args": ["mcp_server.py"],
            "env": {"MINION_DATA_DIR": str(tmpdir), "MINION_BUILD_SHA": "test123"},
        }

        result = conn._upsert_mcp_entry(cfg_path, "minion", create_if_missing=False)

        assert result["action"] == "skipped_missing_config"
        assert result["config_path"] == str(cfg_path)
        assert cfg_path.exists() is False

        # Restore original method
        conn._build_mcp_entry = original_build


def test_list_available():
    """Test listing all available connectors with status."""
    # Clear registry before test
    ConnectorRegistry._connectors.clear()
    
    registry = ConnectorRegistry()

    conn1 = MockConnector("test1", "Test 1", "https://example.com")
    conn1.set_installed(True)
    conn1.set_config_path(Path("/fake/config.json"))

    conn2 = MockConnector("test2", "Test 2", "https://example.com")
    conn2.set_installed(False)

    registry.register(conn1)
    registry.register(conn2)

    available = registry.list_available()
    assert len(available) == 2

    # Find test1
    test1_status = next(c for c in available if c["connector_id"] == "test1")
    assert test1_status["display_name"] == "Test 1"
    assert test1_status["installed"] is True

    # Find test2
    test2_status = next(c for c in available if c["connector_id"] == "test2")
    assert test2_status["display_name"] == "Test 2"
    assert test2_status["installed"] is False
