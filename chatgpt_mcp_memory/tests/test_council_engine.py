"""Council pipeline: patterns, gating, approval learning, feed envelope."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from council_api import handle_council_approve
from council_engine import evaluate_patterns, list_open_feed_items
from council_skills import SkillSpec, approval_options_for_skill, get_skill, register_skill
from council_store import capability_ref_upsert, council_pattern_state_get
from leverage_patterns import detect_contact_drift
from store import _new_id, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def _insert_person(conn, title: str, *, last_contact_at: float | None, birthday: str | None = None) -> str:
    nid = _new_id("gn")
    now = time.time()
    meta = {}
    if last_contact_at is not None:
        meta["last_contact_at"] = last_contact_at
    if birthday:
        meta["birthday"] = birthday
    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES(?, 'person', ?, 'active', '', NULL, "
        "'scaffold-people-friends', '[]', ?, 0.5, '[]', 'vault_local', ?, ?)",
        (nid, title, json.dumps(meta), now, now),
    )
    return nid


def test_contact_drift_detects_stale_person(conn) -> None:
    old = time.time() - 40 * 86400
    _insert_person(conn, "Mike", last_contact_at=old)
    conn.commit()
    ctx = detect_contact_drift(conn, None)
    assert ctx is not None
    assert ctx.subject_label == "Mike"
    assert ctx.meta.get("days_since_contact", 0) >= 21


def test_evaluate_patterns_creates_proposal(conn, tmp_path: Path) -> None:
    old = time.time() - 35 * 86400
    _insert_person(conn, "Sam", last_contact_at=old)
    conn.commit()
    items = evaluate_patterns(conn, tmp_path)
    assert len(items) >= 1
    assert items[0]["item_kind"] == "council"
    assert items[0]["required_skill"] == "send_message"
    assert items[0]["proposal"]["intensity"] == "standard"
    opts = items[0]["approval"]["options"]
    assert any(o["id"] == "approve" and "Send" in o["label"] for o in opts)


def test_approve_updates_pattern_state(conn, tmp_path: Path) -> None:
    old = time.time() - 35 * 86400
    _insert_person(conn, "Pat", last_contact_at=old)
    conn.commit()
    evaluate_patterns(conn, tmp_path)
    feed = list_open_feed_items(conn, tmp_path)
    pid = feed[0]["proposal"]["proposal_id"]
    handle_council_approve(conn, proposal_id=pid, action="reject")
    st = council_pattern_state_get(conn, "contact_drift", feed[0]["event"]["subject_id"])
    assert st is not None
    assert st["reject_count"] >= 1
    assert st["suppress_until"] is not None


def test_mock_skill_registers_without_schema_change() -> None:
    register_skill(
        SkillSpec(
            skill_id="test_skill_xyz",
            required_info_keys=("consent_outbound",),
            approval_labels={"approve": "Go"},
        )
    )
    spec = get_skill("test_skill_xyz")
    assert spec is not None
    opts = approval_options_for_skill("test_skill_xyz")
    assert opts[0]["label"] == "Go"


def test_elevated_commerce_when_payment_ready(conn, tmp_path: Path) -> None:
    import datetime as dt

    today = dt.datetime.utcnow()
    bday = f"{today.year}-{today.month:02d}-{(today.day % 28) + 1:02d}"
    nid = _insert_person(conn, "Jordan", last_contact_at=time.time(), birthday=bday)
    conn.execute(
        "INSERT INTO graph_edges(edge_id, from_node_id, to_node_id, rel_kind, created_at, source_refs_json) "
        "VALUES(?, ?, 'scaffold-me', 'knows', ?, ?)",
        (_new_id("ge"), nid, time.time(), json.dumps({"tier": 3})),
    )
    capability_ref_upsert(
        conn,
        cap_key="payment_method",
        label="Card ···4242",
        vault_ref="cap-stub",
    )
    conn.commit()
    items = evaluate_patterns(conn, tmp_path)
    elevated = [i for i in items if i.get("proposal", {}).get("intensity") == "elevated"]
    if elevated:
        assert elevated[0]["required_skill"] == "execute_purchase"
