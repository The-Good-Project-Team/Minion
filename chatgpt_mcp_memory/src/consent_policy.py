"""Reader-scoped consent defaults + persistence (`consent_policy.json` in data dir).

MCP tools (`ask_minion`) filter retrieved chunks here before returning hits.
Desktop HTTP search stays unfiltered so the human sees their full vault locally.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from store import Hit

log = logging.getLogger(__name__)

# Privacy strata (docs/PRIVACY_MATRIX.md)
STRATUM_RAW_EVIDENCE = "raw_evidence"
STRATUM_SUMMARIES = "summaries"
STRATUM_GRAPH_FACTS = "graph_facts"
STRATUM_WORK_CONTEXT = "work_context"
STRATUM_PREFERENCES = "preferences"
STRATUM_PROJECTIONS = "projections"

READER_STRATA_DEFAULTS: Dict[str, List[str]] = {
    "local_ui": [
        STRATUM_RAW_EVIDENCE,
        STRATUM_SUMMARIES,
        STRATUM_GRAPH_FACTS,
        STRATUM_WORK_CONTEXT,
        STRATUM_PREFERENCES,
        STRATUM_PROJECTIONS,
    ],
    "mcp": [
        STRATUM_SUMMARIES,
        STRATUM_GRAPH_FACTS,
        STRATUM_WORK_CONTEXT,
        STRATUM_PREFERENCES,
        STRATUM_PROJECTIONS,
    ],
    "connector_builder": [STRATUM_RAW_EVIDENCE, STRATUM_SUMMARIES, STRATUM_GRAPH_FACTS, STRATUM_WORK_CONTEXT],
    "export_bundle": [
        STRATUM_GRAPH_FACTS,
        STRATUM_WORK_CONTEXT,
        STRATUM_PREFERENCES,
        STRATUM_PROJECTIONS,
        STRATUM_SUMMARIES,
    ],
}

DEFAULT_POLICY: Dict[str, Any] = {
    "schema_version": 1,
    "readers": {
        "mcp": {
            # Indexed chunks whose source kind matches are withheld from MCP retrieval.
            "deny_chunk_source_kinds": ["ambient", "ambient-ax"],
            # Additional path-based withholding (substring match on normalized paths).
            "deny_path_substrings": ["/screen-memory/", "/ambient/"],
            # Screen-context MCP tools read jsonl separately — allow disabling explicitly.
            "allow_screen_context_tools": True,
            "allowed_strata": list(READER_STRATA_DEFAULTS["mcp"]),
            "max_release_level": 3,
            "release_without_ok_level": 2,
            "release_notice_threshold": 3,
            "releasable_chunk_kinds": ["graph-fact", "screen-event", "ambient-summary"],
        },
        "local_ui": {"allowed_strata": list(READER_STRATA_DEFAULTS["local_ui"]), "max_release_level": 5},
        "connector_builder": {"allowed_strata": list(READER_STRATA_DEFAULTS["connector_builder"]), "max_release_level": 5},
        "export_bundle": {"allowed_strata": list(READER_STRATA_DEFAULTS["export_bundle"]), "max_release_level": 4},
    },
}


def policy_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / "consent_policy.json"


def load_policy(data_dir: Path | str) -> Dict[str, Any]:
    pol = copy.deepcopy(DEFAULT_POLICY)
    p = policy_path(Path(data_dir))
    if not p.is_file():
        return pol
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.warning("consent_policy.json unreadable; using defaults")
        return pol
    try:
        readers = raw.get("readers")
        if isinstance(readers, dict):
            mcp = readers.get("mcp")
            if isinstance(mcp, dict):
                pol["readers"]["mcp"].update(mcp)
    except Exception:
        log.warning("consent_policy.json partial parse failure; merging cautiously")
    return pol


def save_policy(data_dir: Path | str, policy: Dict[str, Any]) -> None:
    p = policy_path(Path(data_dir))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(policy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)


def release_level_for_hit(hit: Hit) -> int:
    """Privacy level 0-5: 3 is releasable work context; 5 is raw evidence."""
    meta = getattr(hit, "meta", None) or {}
    source_meta = getattr(hit, "source_meta", None) or {}
    for raw in (meta.get("release_level"), source_meta.get("release_level")):
        try:
            return max(0, min(5, int(raw)))
        except Exception:
            pass
    kind = (getattr(hit, "kind", "") or "").lower()
    if kind == "graph-fact":
        return 3
    if kind == "screen-event":
        return 3
    if kind == "ambient-summary":
        return 2
    if kind in {"ambient", "ambient-ax"}:
        return 5
    path = (getattr(hit, "path", "") or "").replace("\\", "/")
    if "/ambient/" in path or "/screen-memory/" in path:
        return 5
    return 2


def stratum_for_hit(hit: Hit) -> str:
    kind = (getattr(hit, "kind", "") or "").lower()
    if kind == "graph-fact":
        return STRATUM_GRAPH_FACTS
    if kind in {"screen-event", "ambient-summary"}:
        return STRATUM_WORK_CONTEXT
    if kind in {"ambient", "ambient-ax"}:
        return STRATUM_RAW_EVIDENCE
    if release_level_for_hit(hit) >= 5:
        return STRATUM_RAW_EVIDENCE
    return STRATUM_SUMMARIES


def release_notice(level: int, stratum: str) -> str:
    if level >= 5:
        return "Releasing Level 5/5 raw private evidence."
    if level == 4:
        return "Releasing Level 4/5 sensitive operational context."
    if level == 3:
        return "Releasing Level 3/5 personal work context."
    if level == 2:
        return "Releasing Level 2/5 broad contextual summary."
    return "Releasing Level 1/5 generic context."


def _reader_policy(policy: Dict[str, Any], reader_id: str) -> Dict[str, Any]:
    return (policy.get("readers") or {}).get(reader_id) or {}


def hit_allowed_for_reader(
    hit: Hit,
    policy: Dict[str, Any],
    reader_id: str,
    *,
    release_ok: bool = False,
    approved_release_level: int | None = None,
) -> bool:
    r = _reader_policy(policy, reader_id)
    allowed_strata = r.get("allowed_strata") or READER_STRATA_DEFAULTS.get(reader_id, [])
    stratum = stratum_for_hit(hit)
    if stratum not in allowed_strata:
        return False
    max_release = int(r.get("max_release_level", 5 if reader_id == "local_ui" else 3))
    without_ok = int(r.get("release_without_ok_level", max_release))
    effective_max = min(max_release, int(approved_release_level if release_ok and approved_release_level is not None else without_ok))
    if release_level_for_hit(hit) > effective_max:
        return False
    releasable = {
        str(x).strip().lower()
        for x in (r.get("releasable_chunk_kinds") or [])
        if str(x).strip()
    }
    kind = (getattr(hit, "kind", "") or "").lower()
    if kind in releasable:
        return True
    kinds = [
        str(x).strip().lower()
        for x in (r.get("deny_chunk_source_kinds") or [])
        if str(x).strip()
    ]
    if kind and kind in kinds:
        return False
    subs = [str(x) for x in (r.get("deny_path_substrings") or []) if str(x).strip()]
    path_l = (getattr(hit, "path", "") or "").replace("\\", "/")
    for s in subs:
        if s in path_l:
            return False
    return True


def annotate_hit_for_reader(hit: Hit, policy: Dict[str, Any], reader_id: str) -> Hit:
    level = release_level_for_hit(hit)
    stratum = stratum_for_hit(hit)
    r = _reader_policy(policy, reader_id)
    threshold = int(r.get("release_notice_threshold", 3))
    meta = dict(getattr(hit, "meta", None) or {})
    meta["release_level"] = level
    meta["release_stratum"] = stratum
    if level >= threshold:
        meta["release_notice"] = release_notice(level, stratum)
    return replace(hit, meta=meta)


def hit_allowed_for_mcp(
    hit: Hit,
    policy: Dict[str, Any],
    *,
    release_ok: bool = False,
    approved_release_level: int | None = None,
) -> bool:
    return hit_allowed_for_reader(
        hit,
        policy,
        "mcp",
        release_ok=release_ok,
        approved_release_level=approved_release_level,
    )


def _legacy_hit_allowed_for_mcp(hit: Hit, policy: Dict[str, Any]) -> bool:
    r = (policy.get("readers") or {}).get("mcp") or {}
    kinds = [
        str(x).strip().lower()
        for x in (r.get("deny_chunk_source_kinds") or [])
        if str(x).strip()
    ]
    if hit.kind and hit.kind.lower() in kinds:
        return False
    subs = [str(x) for x in (r.get("deny_path_substrings") or []) if str(x).strip()]
    path_l = (hit.path or "").replace("\\", "/")
    for s in subs:
        if s in path_l:
            return False
    return True


def _release_request_hit(hit: Hit, policy: Dict[str, Any]) -> Hit:
    level = release_level_for_hit(hit)
    stratum = stratum_for_hit(hit)
    meta = {
        "release_required": True,
        "release_level": level,
        "release_stratum": stratum,
        "release_notice": release_notice(level, stratum),
        "approval_instruction": (
            f"Ask the user for explicit OK to release Level {level}/5 context. "
            f"If approved, call ask_minion again with release_ok=true and approved_release_level={level}."
        ),
    }
    return replace(
        hit,
        chunk_id=f"release-request:{level}:{stratum}",
        score=max(float(getattr(hit, "score", 0) or 0), 0.01),
        text=(
            f"{release_notice(level, stratum)} "
            "Relevant Minion context exists, but it is withheld until the user explicitly approves this release."
        ),
        role="system",
        source_id=f"release-request:{level}:{stratum}",
        path=f"release/request/level-{level}/{stratum}",
        kind="release-request",
        meta=meta,
        source_meta={"source": "consent_policy"},
    )


def filter_hits_for_mcp(
    hits: List[Hit],
    data_dir: Path | str,
    *,
    release_ok: bool = False,
    approved_release_level: int | None = None,
) -> List[Hit]:
    pol = load_policy(Path(data_dir))
    out: List[Hit] = []
    release_requests: Dict[tuple[int, str], Hit] = {}
    for h in hits:
        if hit_allowed_for_mcp(
            h,
            pol,
            release_ok=release_ok,
            approved_release_level=approved_release_level,
        ):
            out.append(annotate_hit_for_reader(h, pol, "mcp"))
            continue
        level = release_level_for_hit(h)
        stratum = stratum_for_hit(h)
        max_release = int(_reader_policy(pol, "mcp").get("max_release_level", 3))
        allowed = _reader_policy(pol, "mcp").get("allowed_strata") or READER_STRATA_DEFAULTS["mcp"]
        if stratum in allowed and level <= max_release:
            release_requests.setdefault((level, stratum), _release_request_hit(h, pol))
    return [*release_requests.values(), *out]


def screen_tools_allowed_for_mcp(data_dir: Path | str) -> bool:
    pol = load_policy(Path(data_dir))
    r = (pol.get("readers") or {}).get("mcp") or {}
    return bool(r.get("allow_screen_context_tools", True))


def reader_allowed_strata(reader_id: str, data_dir: Path | str) -> List[str]:
    """Allowed privacy strata for a reader (see docs/PRIVACY_MATRIX.md)."""
    pol = load_policy(Path(data_dir))
    readers = pol.get("readers") or {}
    r = readers.get(reader_id) or {}
    raw = r.get("allowed_strata")
    if isinstance(raw, list) and raw:
        return [str(x) for x in raw]
    return list(READER_STRATA_DEFAULTS.get(reader_id, READER_STRATA_DEFAULTS["mcp"]))


def privacy_matrix() -> Dict[str, Any]:
    """Static matrix for API/docs — not user-editable per stratum yet."""
    return {
        "strata": {
            STRATUM_RAW_EVIDENCE: "Full ambient/screen chunk text",
            STRATUM_SUMMARIES: "Rolled-up ambient summaries",
            STRATUM_GRAPH_FACTS: "Durable graph nodes and edges",
            STRATUM_WORK_CONTEXT: "Releasable current-work summaries (for example Level 3/5 fused screen events)",
            STRATUM_PREFERENCES: "Identity preference claims",
            STRATUM_PROJECTIONS: "Composed context bundles",
        },
        "release_levels": {
            "0": "No personal/context data",
            "1": "Generic state",
            "2": "Broad project category",
            "3": "Specific releasable work context",
            "4": "Sensitive operational detail",
            "5": "Raw private evidence",
        },
        "readers": {
            rid: {
                "allowed_strata": list(s),
                "max_release_level": DEFAULT_POLICY["readers"].get(rid, {}).get("max_release_level", 3),
            }
            for rid, s in READER_STRATA_DEFAULTS.items()
        },
    }
