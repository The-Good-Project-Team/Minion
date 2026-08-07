"""Cursor connector implementation."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from connector_base import Connector


class CursorConnector(Connector):
    """Connector for Cursor MCP integration."""

    @property
    def connector_id(self) -> str:
        return "cursor"

    @property
    def display_name(self) -> str:
        return "Cursor"

    @property
    def download_url(self) -> str:
        return "https://cursor.sh"

    def get_config_path(self, config_path_override: Optional[str] = None) -> Optional[Path]:
        if config_path_override:
            return Path(config_path_override).expanduser().resolve()
        env = os.environ.get("CURSOR_MCP_CONFIG")
        if env:
            return Path(env).expanduser().resolve()
        return Path.home() / ".cursor" / "mcp.json"

    def is_installed(self) -> bool:
        if os.environ.get("MINION_SKIP_CURSOR_APP_CHECK"):
            return True
        if sys.platform == "darwin":
            return any(
                p.is_dir()
                for p in (
                    Path("/Applications/Cursor.app"),
                    Path.home() / "Applications" / "Cursor.app",
                )
            )
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                exe = Path(local) / "Cursor" / "Cursor.exe"
                if exe.is_file():
                    return True
            pf = os.environ.get("ProgramFiles", "")
            if pf:
                exe = Path(pf) / "Cursor" / "Cursor.exe"
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
