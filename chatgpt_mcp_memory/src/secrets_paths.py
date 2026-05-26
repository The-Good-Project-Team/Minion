"""Gitignored secret files (never commit real keys)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

GEMINI_KEY_FILENAME = "gemini_api_key"


def minion_repo_root() -> Path:
    # .../minion/chatgpt_mcp_memory/src/secrets_paths.py -> minion
    return Path(__file__).resolve().parents[2]


def gemini_key_file_candidates(data_dir: Optional[Path] = None) -> Iterator[Path]:
    if data_dir:
        yield Path(data_dir) / ".secrets" / GEMINI_KEY_FILENAME
    yield minion_repo_root() / ".secrets" / GEMINI_KEY_FILENAME


def read_gemini_api_key_file(data_dir: Optional[Path] = None) -> Optional[str]:
    for path in gemini_key_file_candidates(data_dir):
        try:
            if path.is_file():
                v = path.read_text(encoding="utf-8").strip()
                if v and not v.startswith("#"):
                    return v
        except OSError:
            log.debug("read gemini key file %s failed", path, exc_info=True)
    return None
