"""Resolve a persistent directory for fastembed ONNX weights.

fastembed defaults to ``tempfile.gettempdir()/fastembed_cache`` (often under
``/tmp``), which macOS clears and which yields stale HuggingFace snapshot dirs
without ``model.onnx``. Minion stores weights beside the SQLite DB.

Override with ``FASTEMBED_CACHE_PATH`` (absolute path to the cache root).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_registered_root: Optional[Path] = None


def register_fastembed_data_dir(data_dir: Path) -> None:
    """Called from MCP/API bootstrap so ingest (no direct data_dir) matches the DB."""
    global _registered_root
    _registered_root = data_dir.expanduser().resolve()


def _env_first(*names: str) -> Optional[str]:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def fastembed_cache_dir(*, data_dir: Optional[Path] = None) -> str:
    custom = os.environ.get("FASTEMBED_CACHE_PATH")
    if custom:
        p = Path(custom).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    root = data_dir if data_dir is not None else _registered_root
    if root is None:
        env = _env_first("MINION_DATA_DIR", "CHATGPT_MCP_DATA_DIR")
        if env:
            root = Path(env).expanduser().resolve()
        else:
            root = Path(__file__).resolve().parents[1] / "data" / "derived"
    root.mkdir(parents=True, exist_ok=True)
    cache = root / "fastembed_cache"
    cache.mkdir(parents=True, exist_ok=True)
    return str(cache)
