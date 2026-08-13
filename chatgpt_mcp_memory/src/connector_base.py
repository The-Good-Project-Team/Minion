"""Base connector abstraction for AI assistant MCP integrations.

Provides a standardized interface for connecting Minion to various AI assistants
(Claude Desktop, Cursor, Perplexity, Copilot, etc.) via their MCP config systems.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def _minion_data_dir() -> Path:
    """Resolve Minion data dir from API state or environment."""
    env = os.environ.get("MINION_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    try:
        from api import State

        data_dir = getattr(State, "data_dir", None)
        if data_dir is not None:
            return Path(data_dir)
    except Exception:
        pass
    raise RuntimeError("MINION_DATA_DIR is not configured")


class Connector(ABC):
    """Abstract base class for AI assistant connectors.

    Each connector implements the MCP config protocol for a specific AI assistant.
    The interface provides methods for checking installation status, configuration,
    and connecting Minion to the assistant.
    """

    @property
    @abstractmethod
    def connector_id(self) -> str:
        """Unique identifier for this connector (e.g., 'claude-desktop', 'cursor')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for UI display (e.g., 'Claude Desktop', 'Cursor')."""
        pass

    @property
    @abstractmethod
    def download_url(self) -> str:
        """URL where users can download this assistant."""
        pass

    @abstractmethod
    def get_config_path(self, config_path_override: Optional[str] = None) -> Optional[Path]:
        """Return the default MCP config path for this assistant.

        Args:
            config_path_override: Optional user-provided config path

        Returns:
            Path to config file, or None if not resolvable on this platform
        """
        pass

    @abstractmethod
    def is_installed(self) -> bool:
        """Check if the assistant app is installed on this machine."""
        pass

    def is_configured(self, config_path: Optional[Path] = None, server_name: str = "minion") -> bool:
        """Check if Minion is configured in the assistant's MCP config.

        Args:
            config_path: Path to config file (uses default if None)
            server_name: MCP server name to check for

        Returns:
            True if Minion MCP entry exists in config
        """
        if config_path is None:
            config_path = self.get_config_path()
        if not config_path:
            return False
        try:
            raw = config_path.read_text(encoding="utf-8")
            if not raw.strip():
                return False
            config = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return False
        servers = config.get("mcpServers") if isinstance(config, dict) else None
        return isinstance(servers, dict) and server_name in servers

    def configured_minion_profile_id(self, server_name: str = "minion") -> Optional[str]:
        """MINION_PROFILE_ID stored in the assistant MCP config, if any."""
        cfg_path = self.get_config_path()
        if not cfg_path or not cfg_path.is_file():
            return None
        try:
            raw = cfg_path.read_text(encoding="utf-8")
            if not raw.strip():
                return None
            config = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None
        servers = config.get("mcpServers") if isinstance(config, dict) else None
        if not isinstance(servers, dict):
            return None
        entry = servers.get(server_name)
        if not isinstance(entry, dict):
            return None
        env = entry.get("env")
        if not isinstance(env, dict):
            return None
        pid = env.get("MINION_PROFILE_ID")
        return str(pid).strip() if pid else None

    def get_status(self, server_name: str = "minion") -> Dict[str, Any]:
        """Get connection status for this connector.

        Args:
            server_name: MCP server name to check for

        Returns:
            Dict with keys: installed, configured, connected, config_path,
            minion_profile_id, active_profile_id, profile_needs_reconnect
        """
        cfg_path = self.get_config_path()
        installed = self.is_installed()
        configured = bool(cfg_path and self.is_configured(cfg_path, server_name))
        minion_profile_id = self.configured_minion_profile_id(server_name) if configured else None
        active_profile_id: Optional[str] = None
        try:
            from api import State
            from store import profile_get_active

            active_profile_id = profile_get_active(State.conn())
        except Exception:
            pass
        profile_needs_reconnect = bool(
            configured
            and minion_profile_id
            and active_profile_id
            and minion_profile_id != active_profile_id
        )
        return {
            "installed": installed,
            "configured": configured,
            "connected": installed and configured,
            "config_path": str(cfg_path) if cfg_path else None,
            "minion_profile_id": minion_profile_id,
            "active_profile_id": active_profile_id,
            "profile_needs_reconnect": profile_needs_reconnect,
        }

    def connect(
        self,
        server_name: str = "minion",
        config_path_override: Optional[str] = None,
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """Connect Minion to this assistant by upserting MCP entry.

        Args:
            server_name: MCP server name to register
            config_path_override: Optional user-provided config path
            create_if_missing: Create config file if it doesn't exist

        Returns:
            Dict with connection result details

        Raises:
            ValueError: If config path cannot be resolved or config is invalid
            OSError: If config file cannot be written
        """
        if not self.is_installed():
            raise ValueError(
                f"{self.display_name} is not installed on this machine. "
                f"Install it from {self.download_url}, then try connecting again. "
                f"Minion still works on its own — or use LAN MCP with other clients."
            )

        cfg_path = self.get_config_path(config_path_override)
        if cfg_path is None:
            raise ValueError(f"could not resolve {self.display_name} config path")

        try:
            result = self._upsert_mcp_entry(cfg_path, server_name, create_if_missing=create_if_missing)
        except ValueError as e:
            raise ValueError(str(e))
        except OSError as e:
            detail = f"cannot write {cfg_path}: {e.strerror or 'os error'}"
            raise OSError(detail)
        except Exception as e:
            raise RuntimeError(f"connect failed: {e.__class__.__name__}: {e}")

        restart_required = result["action"] != "noop"
        message = (
            f"Minion is connected to {self.display_name}. "
            f"Fully quit and reopen {self.display_name} so it picks up memory tools."
            if restart_required
            else f"{self.display_name} is already connected to Minion."
        )
        return {
            "config_path": result["config_path"],
            "backup_path": result.get("backup_path"),
            "server_name": result["server_name"],
            "restart_required": restart_required,
            "installed": True,
            "configured": True,
            "message": message,
        }

    def refresh_if_configured(self, server_name: str = "minion") -> Optional[Dict[str, Any]]:
        """Refresh MCP entry when config already exists (startup path)."""
        cfg_path = self.get_config_path()
        if cfg_path is None:
            return None
        try:
            return self._upsert_mcp_entry(cfg_path, server_name, create_if_missing=False)
        except Exception:
            log.exception("mcp auto-refresh failed for %s", self.connector_id)
            return None

    def _upsert_mcp_entry(
        self,
        cfg_path: Path,
        server_name: str,
        *,
        create_if_missing: bool,
    ) -> Dict[str, Any]:
        """Idempotently merge Minion's MCP entry into the config.

        Args:
            cfg_path: Path to config file
            server_name: MCP server name to register
            create_if_missing: Create config file if it doesn't exist

        Returns:
            Dict with action, config_path, backup_path, server_name, build_sha
        """
        entry = self._build_mcp_entry()
        build_sha = entry["env"]["MINION_BUILD_SHA"]

        if not cfg_path.exists():
            if not create_if_missing:
                return {
                    "action": "skipped_missing_config",
                    "config_path": str(cfg_path),
                    "server_name": server_name,
                    "build_sha": build_sha,
                    "backup_path": None,
                }
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            config: Dict[str, Any] = {}
            raw_existed = False
        else:
            raw = cfg_path.read_text(encoding="utf-8")
            config = json.loads(raw) if raw.strip() else {}
            raw_existed = True

        if not isinstance(config, dict):
            raise ValueError("config JSON root must be an object")
        servers = config.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError('"mcpServers" must be an object')

        existing = servers.get(server_name)
        if existing == entry:
            return {
                "action": "noop",
                "config_path": str(cfg_path),
                "server_name": server_name,
                "build_sha": build_sha,
                "backup_path": None,
            }

        backup: Optional[Path] = None
        if raw_existed:
            backup = cfg_path.with_suffix(cfg_path.suffix + ".minion.bak")
            shutil.copy2(cfg_path, backup)

        servers[server_name] = entry
        tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
        tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(cfg_path)

        return {
            "action": "created" if existing is None else "refreshed",
            "config_path": str(cfg_path),
            "backup_path": str(backup) if backup else None,
            "server_name": server_name,
            "build_sha": build_sha,
        }

    def _build_mcp_entry(self) -> Dict[str, Any]:
        """Build the MCP server entry for Minion.

        Returns:
            Dict with command, args, and env for MCP server
        """
        from api import State
        from store import profile_get_active

        data_dir = _minion_data_dir()
        mcp_script = Path(__file__).resolve().parent / "mcp_server.py"
        env = {
            "MINION_DATA_DIR": str(data_dir),
            "MINION_BUILD_SHA": self._mcp_build_sha(),
        }
        try:
            conn = State.conn()
            active_profile = profile_get_active(conn)
            if active_profile:
                env["MINION_PROFILE_ID"] = active_profile
        except Exception:
            pass
        return {
            "command": sys.executable,
            "args": [str(mcp_script)],
            "env": env,
        }

    def _mcp_build_sha(self) -> str:
        """Short content hash of MCP-relevant sources.

        Changes here signal the assistant to reconnect for updated tools.
        """
        import hashlib

        h = hashlib.sha256()
        mcp_script = Path(__file__).resolve().parent / "mcp_server.py"
        try:
            h.update(mcp_script.read_bytes())
        except OSError:
            pass
        data_dir = _minion_data_dir()
        for candidate in (
            data_dir / "retrieval_policy.md",
            data_dir.parent / "retrieval_policy.md",
        ):
            try:
                h.update(candidate.read_bytes())
            except OSError:
                pass
        return h.hexdigest()[:16]


class ConnectorRegistry:
    """Registry for discovering and managing AI assistant connectors."""

    _connectors: Dict[str, Connector] = {}

    @classmethod
    def register(cls, connector: Connector) -> None:
        """Register a connector instance.

        Args:
            connector: Connector instance to register
        """
        cls._connectors[connector.connector_id] = connector
        log.debug("Registered connector: %s", connector.connector_id)

    @classmethod
    def get(cls, connector_id: str) -> Optional[Connector]:
        """Get a connector by ID.

        Args:
            connector_id: Unique connector identifier

        Returns:
            Connector instance or None if not found
        """
        return cls._connectors.get(connector_id)

    @classmethod
    def list_all(cls) -> Dict[str, Connector]:
        """Get all registered connectors.

        Returns:
            Dict mapping connector_id to Connector instance
        """
        return dict(cls._connectors)

    @classmethod
    def list_available(cls) -> list[Dict[str, Any]]:
        """Get status info for all registered connectors.

        Returns:
            List of dicts with connector_id, display_name, installed, configured, connected
        """
        return [
            {
                "connector_id": conn.connector_id,
                "display_name": conn.display_name,
                **conn.get_status(),
            }
            for conn in cls._connectors.values()
        ]


def initialize_connectors() -> None:
    """Register all available AI assistant connectors."""
    from connectors import ClaudeDesktopConnector, CursorConnector

    ConnectorRegistry.register(ClaudeDesktopConnector())
    ConnectorRegistry.register(CursorConnector())
    log.info("Initialized %d connectors", len(ConnectorRegistry.list_all()))
