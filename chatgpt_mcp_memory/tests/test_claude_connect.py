"""Claude Desktop connect must not silently succeed when the app is missing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import api as minion_api


def test_connect_claude_desktop_rejects_when_app_missing(monkeypatch, tmp_path: Path) -> None:
    from connector_base import ConnectorRegistry, initialize_connectors

    cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setenv("CLAUDE_DESKTOP_CONFIG", str(cfg))
    monkeypatch.delenv("MINION_SKIP_CLAUDE_APP_CHECK", raising=False)

    ConnectorRegistry._connectors.clear()
    initialize_connectors()
    connector = ConnectorRegistry.get("claude-desktop")
    assert connector is not None
    monkeypatch.setattr(connector, "is_installed", lambda: False)

    with pytest.raises(HTTPException) as exc:
        minion_api.connect_claude_desktop(minion_api.ConnectBody())

    assert exc.value.status_code == 400
    assert "not installed" in str(exc.value.detail).lower()
    assert not cfg.exists()


def test_connect_cursor_rejects_when_app_missing(monkeypatch, tmp_path: Path) -> None:
    from connector_base import ConnectorRegistry, initialize_connectors

    cfg = tmp_path / "cursor_mcp.json"
    monkeypatch.setenv("CURSOR_MCP_CONFIG", str(cfg))
    monkeypatch.delenv("MINION_SKIP_CURSOR_APP_CHECK", raising=False)

    ConnectorRegistry._connectors.clear()
    initialize_connectors()
    connector = ConnectorRegistry.get("cursor")
    assert connector is not None
    monkeypatch.setattr(connector, "is_installed", lambda: False)

    with pytest.raises(HTTPException) as exc:
        minion_api.connect_generic("cursor", minion_api.ConnectBody())

    assert exc.value.status_code == 400
    assert "not installed" in str(exc.value.detail).lower()
    assert not cfg.exists()


def test_refresh_mcp_on_launch_touches_all_connectors(monkeypatch) -> None:
    from connector_base import ConnectorRegistry, initialize_connectors

    ConnectorRegistry._connectors.clear()
    initialize_connectors()
    refreshed: list[str] = []

    def _track_refresh(connector, server_name: str = "minion"):
        refreshed.append(connector.connector_id)
        return None

    for connector in ConnectorRegistry.list_all().values():
        monkeypatch.setattr(connector, "refresh_if_configured", lambda sn="minion", c=connector: _track_refresh(c, sn))

    monkeypatch.delenv("MINION_SKIP_MCP_REFRESH", raising=False)
    minion_api._refresh_mcp_on_launch()
    assert set(refreshed) == {"claude-desktop", "cursor"}


def test_claude_mcp_configured_detects_minion_entry(tmp_path: Path) -> None:
    from connector_base import ConnectorRegistry, initialize_connectors

    ConnectorRegistry._connectors.clear()
    initialize_connectors()
    connector = ConnectorRegistry.get("claude-desktop")
    assert connector is not None

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"minion": {"command": "python", "args": [], "env": {}}}}),
        encoding="utf-8",
    )
    assert connector.is_configured(cfg) is True
    assert connector.is_configured(tmp_path / "missing.json") is False
