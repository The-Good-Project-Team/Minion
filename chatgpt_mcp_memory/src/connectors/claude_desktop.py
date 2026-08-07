"""Claude Desktop connector implementation."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from connector_base import Connector


class ClaudeDesktopConnector(Connector):
    """Connector for Claude Desktop MCP integration."""

    @property
    def connector_id(self) -> str:
        return "claude-desktop"

    @property
    def display_name(self) -> str:
        return "Claude Desktop"

    @property
    def download_url(self) -> str:
        return "https://claude.ai/download"

    def get_config_path(self, config_path_override: Optional[str] = None) -> Optional[Path]:
        if config_path_override:
            return Path(config_path_override).expanduser().resolve()
        env = os.environ.get("CLAUDE_DESKTOP_CONFIG")
        if env:
            return Path(env).expanduser().resolve()
        if sys.platform == "darwin":
            return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
        return Path.home() / ".config/Claude/claude_desktop_config.json"

    def is_installed(self) -> bool:
        if os.environ.get("MINION_SKIP_CLAUDE_APP_CHECK"):
            return True
        if sys.platform == "darwin":
            return any(
                p.is_dir()
                for p in (
                    Path("/Applications/Claude.app"),
                    Path.home() / "Applications" / "Claude.app",
                )
            )
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                exe = Path(local) / "AnthropicClaude" / "claude.exe"
                if exe.is_file():
                    return True
            pf = os.environ.get("ProgramFiles", "")
            if pf:
                exe = Path(pf) / "Claude" / "claude.exe"
                if exe.is_file():
                    return True
            return False
        cfg = self.get_config_path()
        if cfg is not None:
            try:
                cfg.parent.mkdir(parents=True, exist_ok=True)
                return True
            except OSError:
                return False
        return False
