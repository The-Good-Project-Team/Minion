"""Context platform contract — schema version, privacy scopes, bundle enrichment."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

CONTEXT_BUNDLE_SCHEMA_VERSION = 1

# Privacy strata (see docs/PRIVACY_MATRIX.md)
STRATUM_RAW_EVIDENCE = "raw_evidence"
STRATUM_SUMMARIES = "summaries"
STRATUM_GRAPH_FACTS = "graph_facts"
STRATUM_WORK_CONTEXT = "work_context"
STRATUM_PREFERENCES = "preferences"
STRATUM_PROJECTIONS = "projections"

ALL_STRATA = (
    STRATUM_RAW_EVIDENCE,
    STRATUM_SUMMARIES,
    STRATUM_GRAPH_FACTS,
    STRATUM_WORK_CONTEXT,
    STRATUM_PREFERENCES,
    STRATUM_PROJECTIONS,
)

READER_LOCAL_UI = "local_ui"
READER_MCP = "mcp"
READER_CONNECTOR_BUILDER = "connector_builder"
READER_EXPORT = "export_bundle"


def reader_allowed_strata(reader_id: str, data_dir: Path | str) -> List[str]:
    from consent_policy import reader_allowed_strata as _ras

    return _ras(reader_id, data_dir)


def privacy_scope_payload(reader_id: str, data_dir: Path | str) -> Dict[str, Any]:
    allowed = reader_allowed_strata(reader_id, data_dir)
    from consent_policy import load_policy

    reader = (load_policy(data_dir).get("readers") or {}).get(reader_id) or {}
    return {
        "reader": reader_id,
        "allowed_strata": allowed,
        "denied_strata": [s for s in ALL_STRATA if s not in allowed],
        "max_release_level": int(reader.get("max_release_level", 3 if reader_id == READER_MCP else 5)),
        "release_notice": "Level 3/5 personal work context may be shared with this reader."
        if reader_id == READER_MCP and int(reader.get("max_release_level", 3)) >= 3
        else "",
    }


def preferences_snapshot(conn, *, limit: int = 12) -> Dict[str, Any]:
    """Active preference claims + latest clusters for context bundles."""
    from identity import CLAIM_KINDS
    from store import identity_claim_list, preference_clusters_list

    claims = [
        c
        for c in identity_claim_list(conn, status="active", limit=limit * 2)
        if str(c.get("kind") or "") == "preference"
    ][:limit]
    clusters = preference_clusters_list(conn)[:5]
    return {
        "active_claims": [
            {
                "claim_id": c.get("claim_id"),
                "text": (c.get("text") or "")[:300],
                "confidence": c.get("confidence"),
                "source_agent": c.get("source_agent"),
            }
            for c in claims
        ],
        "clusters": [
            {"cluster_id": cl.get("cluster_id"), "label": cl.get("label"), "summary": (cl.get("summary") or "")[:200]}
            for cl in clusters
        ],
        "claim_kinds_supported": sorted(CLAIM_KINDS),
    }


def connector_intents_snapshot(conn, *, limit: int = 8) -> List[Dict[str, Any]]:
    from connector_intent import list_open_connector_work

    return list_open_connector_work(conn, limit=limit)


def resource_poll_snapshot(data_dir: Path) -> Dict[str, Any]:
    from connector_intent import load_resource_poll

    return load_resource_poll(data_dir)


def session_snapshot(data_dir: Path) -> Optional[Dict[str, Any]]:
    from session_open import get_session_hint, session_hint_from_disk

    hint = get_session_hint() or session_hint_from_disk(Path(data_dir))
    if not hint:
        return None
    return hint


def enrich_context_bundle(
    conn,
    data_dir: Path,
    bundle: Dict[str, Any],
    *,
    reader_id: str,
) -> Dict[str, Any]:
    """Attach platform contract fields to an existing context_bundle dict."""
    out = dict(bundle)
    out["schema_version"] = CONTEXT_BUNDLE_SCHEMA_VERSION
    out["privacy_scope"] = privacy_scope_payload(reader_id, data_dir)
    out["preferences"] = preferences_snapshot(conn)
    out["connector_intents"] = connector_intents_snapshot(conn)
    out["resource_poll"] = resource_poll_snapshot(data_dir)
    session = session_snapshot(data_dir)
    if session:
        out["session"] = session
    out["platform"] = {
        "layers": ["vault", "context_server", "world_model", "live_preferences"],
        "doc": "docs/CONTEXT_PLATFORM.md",
    }
    return out
