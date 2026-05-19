"""Tests for storage_tier ordering bias (search-fast-fresh / tier compaction)."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from retrieval_bias import apply_identity_rerank  # noqa: E402
from store import Hit, connect  # noqa: E402


def _hit(chunk_id: str, score: float, tier: str) -> Hit:
    return Hit(
        chunk_id=chunk_id,
        score=score,
        text="hello world",
        role=None,
        source_id="s1",
        path="/tmp/x.md",
        kind="text",
        mtime=0.0,
        meta={},
        source_meta={},
        storage_tier=tier,
    )


def test_warm_sorts_after_hot_at_same_cosine(tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    warm_first = [_hit("c_warm", 0.8, "warm"), _hit("c_hot", 0.8, "hot")]
    out, meta = apply_identity_rerank(conn, warm_first)
    assert [h.chunk_id for h in out] == ["c_hot", "c_warm"]
    assert meta["tier_bias_non_hot"] == 1


def test_cold_behind_warm_at_same_cosine(tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    xs = [_hit("c_cold", 0.8, "cold"), _hit("c_warm", 0.8, "warm"), _hit("c_hot", 0.8, "hot")]
    out, meta = apply_identity_rerank(conn, xs)
    assert [h.chunk_id for h in out] == ["c_hot", "c_warm", "c_cold"]
    assert meta["tier_bias_non_hot"] == 2
