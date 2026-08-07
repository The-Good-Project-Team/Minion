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


def test_claude_mcp_configured_detects_minion_entry(tmp_path: Path) -> None:
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"minion": {"command": "python", "args": [], "env": {}}}}),
        encoding="utf-8",
    )
    assert minion_api._claude_mcp_configured(cfg) is True
    assert minion_api._claude_mcp_configured(tmp_path / "missing.json") is False
