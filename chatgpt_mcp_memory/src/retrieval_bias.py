"""Bounded re-ranking from identity signals (latest cluster run + active claims).

Sort key = cosine score + optional storage-tier freshness epsilon + small capped
identity boosts; Hit.score stays the real cosine so telemetry and UI stay
comparable to AGENTS.md invariants.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple, TypeVar

from store import Hit, identity_claim_list, preference_clusters_list

HitT = TypeVar("HitT")

# Canonical RRF k (Cormack et al. 2009); matches mcp_server._RRF_K.
_RRF_K = 60


def rrf_fuse_many(
    lanes: List[Tuple[List[HitT], float]],
    *,
    k: int = _RRF_K,
) -> List[HitT]:
    """Weighted Reciprocal Rank Fusion over N ranked lanes.

    `lanes` is an ordered list of (hits, weight). On an overlapping chunk_id the
    Hit object from the *earliest* lane is kept, so callers should order the lane
    whose Hit.score they want preserved (the semantic/cosine lane) first — this
    keeps Hit.score comparable for telemetry/UI per the module invariant.
    """
    scores: Dict[str, float] = {}
    kept: Dict[str, HitT] = {}
    for hits, weight in lanes:
        for rank, h in enumerate(hits, start=1):
            cid = h.chunk_id  # type: ignore[attr-defined]
            scores[cid] = scores.get(cid, 0.0) + weight / (k + rank)
            kept.setdefault(cid, h)
    return sorted(kept.values(), key=lambda h: scores[h.chunk_id], reverse=True)  # type: ignore[attr-defined]


def rrf_fuse(
    relevance_hits: List[HitT],
    keyword_hits: List[HitT],
    *,
    k: int = _RRF_K,
    semantic_weight: float = 1.5,
) -> List[HitT]:
    """Two-lane weighted RRF; semantic list wins on overlapping chunk_id (cosine on Hit).

    Backwards-compatible thin wrapper over `rrf_fuse_many`.
    """
    return rrf_fuse_many(
        [(relevance_hits, semantic_weight), (keyword_hits, 1.0)], k=k
    )


_RECENCY_MAX_EPS = 0.02
_RECENCY_HALFLIFE_DAYS = 30.0


def _recency_decay_enabled() -> bool:
    import os

    return os.environ.get("MINION_DISABLE_RECENCY_DECAY", "").strip().lower() not in (
        "1", "true", "yes",
    )


def _recency_sort_epsilon(mtime: float, now: float) -> float:
    """Gentle, ALWAYS-POSITIVE recency lift (sort-key only; Hit.score untouched).

    eps = max_eps * 0.5^(age/halflife): a fresh memory gets +0.02, ~30d +0.01, old ~0.
    It lifts recent hits on near-ties; it never demotes older relevant hits below them.
    """
    if not mtime or mtime <= 0:
        return 0.0
    age_days = max(0.0, (now - float(mtime)) / 86400.0)
    return _RECENCY_MAX_EPS * (0.5 ** (age_days / _RECENCY_HALFLIFE_DAYS))


def _storage_tier_sort_epsilon(storage_tier: str) -> float:
    """Ordering bias only: warm/cold sink slightly vs hot when cosine ties; Hit.score unchanged."""
    t = (storage_tier or "hot").strip().lower()
    if t == "warm":
        return -0.015
    if t == "cold":
        return -0.035
    return 0.0


_STOP = frozenset(
    """
    the and for are but not you all can had her was one our out day get has
    him his how its may new now see two way who boy did its let put say she
    too use any few per ran sit try why ask own seem than that this with
    have from they been call each make like some time very when what which
    your about after again could every first being would there their where
    other these those under while those into over such only also just more
    most much than then them well will
    """.split()
)


def _tokens(text: str) -> Set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOP}


def _latest_cluster_members(
    conn: sqlite3.Connection,
) -> Tuple[Set[str], Optional[float], int]:
    rows = preference_clusters_list(conn)
    if not rows:
        return set(), None, 0
    latest = float(rows[0]["run_at"])
    members: Set[str] = set()
    n_clusters = 0
    for r in rows:
        if float(r["run_at"]) != latest:
            break
        n_clusters += 1
        members.update(r.get("member_chunk_ids") or [])
    return members, latest, n_clusters


def _active_claim_tokens(
    conn: sqlite3.Connection, max_claims: int = 40
) -> Tuple[Set[str], int]:
    claims = identity_claim_list(conn, status="active", limit=max_claims)
    bag: Set[str] = set()
    for c in claims:
        bag |= _tokens(c.get("text") or "")
    return bag, len(claims)


def apply_identity_rerank(
    conn: sqlite3.Connection,
    hits: List[Hit],
    *,
    cluster_boost: float = 0.04,
    claim_coeff: float = 0.12,
    claim_cap: float = 0.04,
    total_cap: float = 0.07,
) -> Tuple[List[Hit], Dict[str, Any]]:
    """Re-order hits: storage-tier epsilon + small capped identity boosts; Hit.score unchanged."""
    meta: Dict[str, Any] = {
        "bias_clusters": 0,
        "bias_claims": 0,
        "bias_run_at": None,
        "adjustments_applied": 0,
        "tier_bias_non_hot": 0,
    }
    if not hits:
        return hits, meta

    members, run_at, n_cl = _latest_cluster_members(conn)
    claim_toks, n_claims = _active_claim_tokens(conn)
    meta["bias_clusters"] = n_cl
    meta["bias_claims"] = n_claims
    meta["bias_run_at"] = run_at

    adjusted_n = 0
    tier_bias_n = 0
    now = time.time()
    recency_on = _recency_decay_enabled()
    keyed: List[Tuple[float, int, Hit]] = []
    for i, h in enumerate(hits):
        tier_eps = _storage_tier_sort_epsilon(getattr(h, "storage_tier", "hot"))
        if tier_eps != 0.0:
            tier_bias_n += 1
        rec_eps = _recency_sort_epsilon(getattr(h, "mtime", 0.0), now) if recency_on else 0.0
        boost = 0.0
        if members and h.chunk_id in members:
            boost += cluster_boost
        if claim_toks:
            htoks = _tokens(h.text)
            if htoks:
                inter = len(htoks & claim_toks)
                if inter:
                    ratio = inter / max(1, len(htoks))
                    boost += min(claim_cap, ratio * claim_coeff)
        boost = min(boost, total_cap)
        if boost > 0:
            adjusted_n += 1
        keyed.append((h.score + tier_eps + rec_eps + boost, -i, h))

    meta["adjustments_applied"] = adjusted_n
    meta["tier_bias_non_hot"] = tier_bias_n
    keyed.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [t[2] for t in keyed], meta
