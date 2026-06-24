"""Shared helpers for parsers (text normalization + chunking)."""
from __future__ import annotations

import os
import re
from typing import List, Dict, Any

# Default chunk ceiling, in characters. Sized to the embedder's context budget:
# bge-base-en-v1.5 (and the ms-marco cross-encoder) cap at 512 tokens ≈ ~2000
# chars of English. The old 1200 both FRAGMENTED short docs (a ~1.5-1.8k-char
# abstract split into 2 half-context chunks, tanking dense recall — measured
# nDCG@10 0.34 chunked vs 0.74 whole on BEIR/scifact) AND wasted ~40% of the
# model's window. Keep short docs whole; only genuinely long docs still split.
# Override with MINION_CHUNK_MAX_CHARS for corpora with a different embedder.
def _default_chunk_max_chars() -> int:
    try:
        v = int(os.environ.get("MINION_CHUNK_MAX_CHARS", "") or 0)
        return v if v > 0 else 2000
    except ValueError:
        return 2000


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def chunk_text(text: str, *, max_chars: int | None = None) -> List[str]:
    """Paragraph-aware splitter with sentence fallback for oversize paragraphs."""
    if max_chars is None:
        max_chars = _default_chunk_max_chars()
    text = normalize_text(text)
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current and current_len + para_len + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len + (2 if current_len else 0)

    if current:
        chunks.append("\n\n".join(current))

    final: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final.append(chunk)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", chunk)
        cur: List[str] = []
        cur_len = 0
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if cur and cur_len + len(sentence) + 1 > max_chars:
                final.append(" ".join(cur))
                cur = [sentence]
                cur_len = len(sentence)
            else:
                cur.append(sentence)
                cur_len += len(sentence) + (1 if cur_len else 0)
        if cur:
            final.append(" ".join(cur))

    return [c for c in final if c.strip()]


def window_text(text: str, *, max_chars: int = 1200) -> List[str]:
    """Hard window splitter for structured content (code, logs) where paragraph
    boundaries are unreliable."""
    text = text.replace("\r\n", "\n")
    if not text:
        return []
    lines = text.split("\n")
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for line in lines:
        if cur and cur_len + len(line) + 1 > max_chars:
            chunks.append("\n".join(cur))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def chunk_conversation(messages: List[Dict[str, Any]], *, max_chars: int | None = None) -> List[str]:
    """Chunk a conversation thread while preserving message boundaries.
    
    Args:
        messages: List of message dicts with 'role' and 'text' keys
        max_chars: Maximum characters per chunk (defaults to MINION_CHUNK_MAX_CHARS)
    
    Returns:
        List of chunked text strings, each containing complete messages where possible
    """
    if max_chars is None:
        max_chars = _default_chunk_max_chars()
    
    if not messages:
        return []
    
    chunks: List[str] = []
    current_chunk: List[str] = []
    current_len = 0
    
    for msg in messages:
        role = msg.get("role", "unknown")
        text = msg.get("text", "").strip()
        if not text:
            continue
        
        # Format message with role prefix
        formatted_msg = f"{role.capitalize()}: {text}"
        msg_len = len(formatted_msg)
        
        # If adding this message would exceed the limit and we have content,
        # finalize the current chunk
        if current_chunk and current_len + msg_len + 2 > max_chars:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [formatted_msg]
            current_len = msg_len
        else:
            current_chunk.append(formatted_msg)
            current_len += msg_len + (2 if current_len > 0 else 0)
    
    if current_chunk:
        chunks.append("\n\n".join(current_chunk))
    
    return [c for c in chunks if c.strip()]
