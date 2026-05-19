"""Capability key linkage (secrets live in macOS Keychain via desktop)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from council_store import capability_ref_list, capability_ref_upsert


def list_capabilities(conn) -> List[Dict[str, Any]]:
    return capability_ref_list(conn)


def link_capability(
    conn,
    *,
    cap_key: str,
    vault_ref: str,
    label: str,
    provider: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not vault_ref.startswith("keychain:"):
        vault_ref = f"keychain:{vault_ref}"
    ref_id = capability_ref_upsert(
        conn,
        cap_key=cap_key,
        label=label,
        vault_ref=vault_ref,
        provider=provider,
        meta=meta,
    )
    return {"ok": True, "ref_id": ref_id, "cap_key": cap_key, "vault_ref": vault_ref}


def unlink_capability(conn, *, cap_key: str) -> bool:
    import time

    cur = conn.execute(
        "UPDATE capability_refs SET status='inactive', updated_at=? WHERE cap_key=? AND status='active'",
        (time.time(), cap_key),
    )
    return cur.rowcount > 0
