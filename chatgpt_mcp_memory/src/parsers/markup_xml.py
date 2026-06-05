"""XML parser, WordPress-WXR aware.

A WordPress / Squarespace "WXR" export is one big RSS-flavoured XML where every
page/post is an ``<item>`` whose body lives in a ``<content:encoded>`` CDATA
block of HTML. Parsing the whole file as plain text (the old fallback) produced
tag soup; this parser instead emits one clean document per ``<item>`` — title +
de-HTML'd body — so each page becomes its own searchable unit.

Non-WXR XML falls back to a streaming text extraction (all element text), which
is still far better than raw markup. Parsing is streamed via ``iterparse`` with
element clearing so a large export stays within bounded memory.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from . import ParsedChunk, ParseResult
from ._common import chunk_text
from .markup_html import html_to_text


class EmptyParse(ValueError):
    """Raised when the XML yields no usable text (ingest skips cleanly)."""


# Tags we read out of each WXR <item>, matched by *local* name so we stay
# agnostic to the WXR version (export/1.0, 1.1, 1.2 all differ by namespace URI).
_SNIFF_BYTES = 16384


def _local(tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree puts on qualified tags."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _looks_like_feed(path: Path) -> bool:
    """Cheap head sniff: is this a WordPress/RSS export with <item> entries?"""
    try:
        with path.open("rb") as fh:
            head = fh.read(_SNIFF_BYTES).decode("utf-8", errors="replace").lower()
    except OSError:
        return False
    return (
        "wp:wxr_version" in head
        or "xmlns:wp" in head
        or "<rss" in head
        or "<item" in head
    )


def _item_to_text(elem: ET.Element) -> Tuple[str, Dict[str, object]]:
    """Pull (text, meta) out of one WXR <item> element."""
    title = ""
    link = ""
    post_type = ""
    status = ""
    body_html = ""
    excerpt_html = ""

    for child in elem:
        ln = _local(child.tag)
        ns = _ns(child.tag)
        val = (child.text or "").strip()
        if ln == "title" and not title:
            title = val
        elif ln == "link" and not link:
            link = val
        elif ln == "post_type":
            post_type = val
        elif ln == "status":
            status = val
        elif ln == "encoded":
            # content:encoded -> body; excerpt:encoded -> excerpt. Distinguish
            # by namespace URI (the local name is "encoded" for both).
            if "excerpt" in ns:
                excerpt_html = child.text or ""
            else:
                body_html = child.text or ""

    body = html_to_text(body_html).strip() if body_html else ""
    excerpt = html_to_text(excerpt_html).strip() if excerpt_html else ""

    # A document needs actual content — a title alone is a placeholder/nav entry
    # (Squarespace exports are full of them) and would only add search noise.
    if not (body or excerpt):
        return "", {}

    parts: List[str] = []
    if title:
        parts.append(title)
    if excerpt and excerpt not in body:
        parts.append(excerpt)
    if body:
        parts.append(body)
    text = "\n\n".join(parts).strip()

    meta: Dict[str, object] = {}
    if title:
        meta["item_title"] = title
    if link:
        meta["item_link"] = link
    if post_type:
        meta["post_type"] = post_type
    if status:
        meta["status"] = status
    return text, meta


def _iter_items(path: Path) -> Iterator[Tuple[str, Dict[str, object]]]:
    """Stream each <item> as (text, meta), clearing elements to bound memory."""
    context = ET.iterparse(str(path), events=("start", "end"))
    _, root = next(context)  # grab the root so we can drop processed children
    for event, elem in context:
        if event != "end" or _local(elem.tag) != "item":
            continue
        text, meta = _item_to_text(elem)
        if text:
            yield text, meta
        elem.clear()
        root.clear()  # release accumulated siblings — keeps memory flat


def _stream_all_text(path: Path) -> str:
    """Generic (non-WXR) fallback: concatenate every element's text/tail."""
    buf: List[str] = []
    context = ET.iterparse(str(path), events=("start", "end"))
    _, root = next(context)
    for event, elem in context:
        if event != "end":
            continue
        if elem.text and elem.text.strip():
            buf.append(elem.text.strip())
        if elem.tail and elem.tail.strip():
            buf.append(elem.tail.strip())
        elem.clear()
        root.clear()
    return "\n".join(buf)


def parse(path: Path) -> ParseResult:
    chunks: List[ParsedChunk] = []

    if _looks_like_feed(path):
        try:
            item_count = 0
            for text, meta in _iter_items(path):
                item_count += 1
                for j, t in enumerate(chunk_text(text)):
                    cmeta = dict(meta)
                    cmeta["seq"] = j
                    chunks.append(ParsedChunk(text=t, role="page", meta=cmeta))
            if chunks:
                return ParseResult(
                    chunks=chunks,
                    source_meta={"suffix": path.suffix.lower(), "items": item_count},
                    kind="xml",
                    parser="wxr",
                )
        except ET.ParseError as e:
            raise EmptyParse(f"malformed XML: {e}") from e

    # Generic XML (or a feed that yielded no items): strip to plain text.
    try:
        text = _stream_all_text(path)
    except ET.ParseError as e:
        raise EmptyParse(f"malformed XML: {e}") from e
    chunks = [
        ParsedChunk(text=t, role=None, meta={"seq": i})
        for i, t in enumerate(chunk_text(text))
    ]
    if not chunks:
        raise EmptyParse("xml: no extractable text")
    return ParseResult(
        chunks=chunks,
        source_meta={"suffix": path.suffix.lower()},
        kind="xml",
        parser="xml-text",
    )
