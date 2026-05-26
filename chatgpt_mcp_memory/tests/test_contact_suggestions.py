"""42 uses Contacts evidence for bucket-fill suggestions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph_fill import compose_question, contact_suggestions_for_gap, open_thread_for_gap, pick_next_gap
from store import connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_contact_suggestions_exclude_existing(conn, tmp_path: Path) -> None:
    data = tmp_path / "data"
    ev = data / "life_evidence"
    ev.mkdir(parents=True)
    (ev / "contacts_latest.json").write_text(
        json.dumps(
            [
                {"display_name": "Alex Kim"},
                {"display_name": "Jordan Lee"},
            ]
        ),
        encoding="utf-8",
    )
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    gap = {
        "gap_type": "bucket",
        "parent_node_id": "scaffold-people-friends",
        "node_kind": "person",
        "bucket_label": "Friends",
        "hint": "a friend",
    }
    suggestions = contact_suggestions_for_gap(conn, gap, data)
    names = {s["name"] for s in suggestions}
    assert "Alex Kim" in names
    assert "Jordan Lee" in names

    conn.execute(
        "INSERT INTO graph_nodes(node_id, node_kind, title, status, body_md, wiki_page_id, "
        "parent_node_id, aliases_json, summary, confidence, source_refs_json, privacy_level, "
        "created_at, updated_at) VALUES('gn1', 'person', 'Alex Kim', 'active', '', NULL, "
        "'scaffold-people-friends', '[]', '', 0.5, '[]', 'vault_local', 1, 1)"
    )
    conn.commit()
    suggestions2 = contact_suggestions_for_gap(conn, gap, data)
    assert all(s["name"] != "Alex Kim" for s in suggestions2)


def test_compose_bucket_question_mentions_contacts(conn, tmp_path: Path) -> None:
    data = tmp_path / "data"
    ev = data / "life_evidence"
    ev.mkdir(parents=True)
    (ev / "contacts_latest.json").write_text(
        json.dumps([{"display_name": "Sam Rivera"}]),
        encoding="utf-8",
    )
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    gap = pick_next_gap(conn, data)
    assert gap and gap["gap_type"] == "bucket"
    body = compose_question(conn, gap, data_dir=data)
    assert "Sam Rivera" in body
    assert "Contacts" in body

    out = open_thread_for_gap(conn, gap, data_dir=data)
    msg = out["thread"]["messages"][0]
    assert (msg.get("meta") or {}).get("suggestions")
