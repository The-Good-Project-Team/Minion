"""SSE helpers for chat streaming."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator


def sse_line(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def iter_text_deltas(text: str, *, chunk_size: int = 28) -> Iterator[str]:
    t = text or ""
    for i in range(0, len(t), chunk_size):
        yield t[i : i + chunk_size]
