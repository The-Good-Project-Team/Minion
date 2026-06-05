"""
Parser registry. Each parser turns one on-disk file into a list of
ParsedChunk(text, role, meta). Heavy deps (whisper, tree-sitter, ocr models)
are imported lazily inside each parser so core installs stay tiny.

The dispatcher picks a parser by extension first, mimetype second. Override
by passing `parser=...` when calling `parse_file`.
"""
from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from parser_extensions import load_manifest_file, manifest_path

log = logging.getLogger("minion.parsers")


@dataclass
class ParsedChunk:
    text: str
    role: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    """Return value from a parser: the chunks plus source-level metadata."""

    chunks: List[ParsedChunk]
    source_meta: Dict[str, Any] = field(default_factory=dict)
    kind: str = "unknown"
    parser: str = "unknown"


ParserFn = Callable[[Path], ParseResult]


# Extension -> (kind, module_path, function_name). Lazy-loaded via importlib
# so e.g. missing faster-whisper only errors when someone drops audio in.
_EXT_REGISTRY: Dict[str, Tuple[str, str, str]] = {
    # Plaintext / markup
    ".txt":  ("text", "parsers.text", "parse"),
    ".md":   ("text", "parsers.text", "parse"),
    ".markdown": ("text", "parsers.text", "parse"),
    ".rst":  ("text", "parsers.text", "parse"),
    ".org":  ("text", "parsers.text", "parse"),
    ".log":  ("text", "parsers.text", "parse"),
    ".csv":  ("text", "parsers.text", "parse"),
    ".tsv":  ("text", "parsers.text", "parse"),
    ".json": ("text", "parsers.text", "parse"),
    ".yaml": ("text", "parsers.text", "parse"),
    ".yml":  ("text", "parsers.text", "parse"),
    ".toml": ("text", "parsers.text", "parse"),
    ".ini":  ("text", "parsers.text", "parse"),
    # HTML
    ".html": ("html", "parsers.markup_html", "parse"),
    ".htm":  ("html", "parsers.markup_html", "parse"),
    # XML / WordPress-WXR exports (per-<item> page extraction; generic fallback)
    ".xml":  ("xml", "parsers.markup_xml", "parse"),
    ".rss":  ("xml", "parsers.markup_xml", "parse"),
    # PDF
    ".pdf":  ("pdf",  "parsers.pdf",  "parse"),
    # Office docs
    ".docx": ("docx", "parsers.docx", "parse"),
    # Images
    ".png":  ("image", "parsers.image", "parse"),
    ".jpg":  ("image", "parsers.image", "parse"),
    ".jpeg": ("image", "parsers.image", "parse"),
    ".webp": ("image", "parsers.image", "parse"),
    ".bmp":  ("image", "parsers.image", "parse"),
    ".tif":  ("image", "parsers.image", "parse"),
    ".tiff": ("image", "parsers.image", "parse"),
    # Audio (speech-only parser; faster-whisper transcription).
    ".mp3":  ("audio", "parsers.audio", "parse"),
    ".wav":  ("audio", "parsers.audio", "parse"),
    ".m4a":  ("audio", "parsers.audio", "parse"),
    ".flac": ("audio", "parsers.audio", "parse"),
    ".ogg":  ("audio", "parsers.audio", "parse"),
    ".opus": ("audio", "parsers.audio", "parse"),
    # Video (scene-aware: whisper transcript + keyframe OCR + optional caption).
    ".mp4":  ("video", "parsers.video", "parse"),
    ".mov":  ("video", "parsers.video", "parse"),
    ".webm": ("video", "parsers.video", "parse"),
    ".mkv":  ("video", "parsers.video", "parse"),
    ".avi":  ("video", "parsers.video", "parse"),
    ".m4v":  ("video", "parsers.video", "parse"),
    # NOTE: archives (.zip, .tar, .tar.gz) are NOT parsers -- they're
    # unpacked in-place by the ingest layer and their contents flow
    # through the normal per-file dispatch. See ingest._maybe_unpack_archive.
}


# Common code extensions. tree-sitter-language-pack handles dozens; we map
# the popular ones and let the code parser fall back to line-window chunking
# for anything with an unknown grammar.
_CODE_EXT = {
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go",
    ".rs",
    ".java", ".kt", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".m", ".mm",
    ".sh", ".bash", ".zsh",
    ".lua",
    ".r", ".R",
    ".sql",
    ".dart",
    ".ex", ".exs",
    ".erl",
    ".hs",
    ".clj", ".cljs",
    ".vue", ".svelte",
}
for _ext in _CODE_EXT:
    _EXT_REGISTRY[_ext] = ("code", "parsers.code", "parse")

# Filled from ``<data_dir>/parser_extensions.json`` via ``load_user_extensions``.
_USER_EXT_REGISTRY: Dict[str, Tuple[str, str, str]] = {}


def supported_extensions() -> List[str]:
    return sorted(set(_EXT_REGISTRY.keys()) | set(_USER_EXT_REGISTRY.keys()))


def load_user_extensions(data_dir: Path) -> int:
    """Merge user extension manifest into the runtime registry. Returns count added."""
    global _USER_EXT_REGISTRY
    _USER_EXT_REGISTRY = {}
    path = manifest_path(Path(data_dir))
    loaded = load_manifest_file(path)
    added = 0
    for suf, spec in loaded.items():
        if suf in _EXT_REGISTRY:
            log.warning(
                "parser_extensions: suffix %s is built-in; user mapping ignored", suf
            )
            continue
        _USER_EXT_REGISTRY[suf] = spec
        added += 1
    return added


def user_extension_mappings() -> Dict[str, Tuple[str, str, str]]:
    return dict(_USER_EXT_REGISTRY)


def reload_user_extensions(data_dir: Path) -> int:
    """Public alias for tests and ``POST /extensions/reload``."""
    return load_user_extensions(data_dir)


# Canonical list of user-togglable kinds (surfaced in the UI settings pane).
# Keep this exact — the frontend reads it verbatim.
ALL_KINDS: Tuple[str, ...] = (
    "text",
    "html",
    "pdf",
    "docx",
    "image",
    "audio",
    "video",
    "code",
    "chatgpt-export",
    "claude-export",
    "external",
)

# Kinds the user has opted out of. Runtime only; persisted by settings.py.
_DISABLED_KINDS: Set[str] = set()


def disabled_kinds() -> Set[str]:
    return set(_DISABLED_KINDS)


def set_disabled_kinds(kinds: Iterable[str]) -> None:
    _DISABLED_KINDS.clear()
    _DISABLED_KINDS.update(k for k in kinds if k in ALL_KINDS)


def kind_for(path: Path) -> Optional[str]:
    """Return the canonical kind for `path` (or None if unsupported)."""
    chosen = choose_parser(path)
    return chosen[0] if chosen else None


def is_disabled_kind(path: Path) -> bool:
    """True if the user has turned this file's kind off in settings."""
    k = kind_for(path)
    return bool(k and k in _DISABLED_KINDS)


def choose_parser(path: Path) -> Optional[Tuple[str, str, str]]:
    """Return (kind, module, fn) for `path`, or None if unsupported."""
    suffix = path.suffix.lower()
    if suffix in _EXT_REGISTRY:
        return _EXT_REGISTRY[suffix]
    if suffix in _USER_EXT_REGISTRY:
        return _USER_EXT_REGISTRY[suffix]

    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        if mime.startswith("text/html"):
            return _EXT_REGISTRY[".html"]
        if mime.startswith("application/pdf"):
            return _EXT_REGISTRY[".pdf"]
        if mime.startswith("text/"):
            return _EXT_REGISTRY[".txt"]
        if mime.startswith("image/"):
            return _EXT_REGISTRY[".png"]
        if mime.startswith("video/"):
            return _EXT_REGISTRY[".mp4"]
        if mime.startswith("audio/"):
            return _EXT_REGISTRY[".mp3"]
    return None


def parse_file(path: Path, *, parser: Optional[str] = None, on_progress=None) -> ParseResult:
    """Dispatch to the right parser. Raises UnsupportedFile if nothing matches.

    If the concrete parser's `parse()` function accepts an `on_progress`
    keyword, we pass it through so long-running parsers (ChatGPT exports,
    multi-page PDFs) can stream sub-file progress.
    """
    path = Path(path)
    if parser:
        module_path, fn_name = parser, "parse"
        kind = "override"
    else:
        chosen = choose_parser(path)
        if not chosen:
            raise UnsupportedFile(f"No parser for {path.suffix or path.name}")
        kind, module_path, fn_name = chosen

    import importlib
    import inspect

    mod = importlib.import_module(module_path)
    fn: ParserFn = getattr(mod, fn_name)
    kwargs = {}
    if on_progress is not None:
        try:
            sig = inspect.signature(fn)
            if "on_progress" in sig.parameters:
                kwargs["on_progress"] = on_progress
        except (TypeError, ValueError):
            pass
    result = fn(path, **kwargs)
    if not result.kind or result.kind == "unknown":
        result.kind = kind
    return result


class UnsupportedFile(Exception):
    """Raised when no parser matches the file."""
