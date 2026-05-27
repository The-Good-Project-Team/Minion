"""Person identity resolution across local evidence."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from entity_resolution import ensure_person_node, ingest_contacts_snapshot
from life_evidence_index import ingest_life_evidence
from graph_fill import apply_graph_candidate_resolution
from store import connect, graph_candidate_list, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "memory.db")
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_person_resolution_matches_phone_across_sources(conn) -> None:
    a = ensure_person_node(
        conn,
        label="Alex Kim",
        meta={"phone": "+1 (555) 123-9999", "source": "contacts"},
    )
    b = ensure_person_node(
        conn,
        label="A. Kim",
        meta={"imessage": "5551239999", "source": "messages_export"},
    )
    assert a == b


def test_person_resolution_creates_merge_candidate_for_conflicting_identifier(conn) -> None:
    ingest_contacts_snapshot(
        conn,
        [{"display_name": "Jordan Lee", "id": "contact-a", "source": "contacts"}],
    )
    conn.commit()
    nid = ensure_person_node(
        conn,
        label="Jordan Lee",
        external_id="message-handle-b",
        meta={"source": "messages_export", "evidence_refs": ["chunk:m1"]},
    )
    conn.commit()

    candidates = graph_candidate_list(conn)
    assert candidates
    assert candidates[0]["candidate_type"] == "person_merge"
    assert candidates[0]["payload"]["existing_node_id"] == nid
    assert candidates[0]["payload"]["reasoning"]
    assert "No shared hard identifier" in candidates[0]["body_md"]
    assert "chunk:m1" in candidates[0]["evidence_refs"]


def test_approved_person_merge_writes_incoming_identifiers_to_graph(conn, tmp_path: Path) -> None:
    ingest_contacts_snapshot(
        conn,
        [{"display_name": "Jordan Lee", "id": "contact-a", "source": "contacts"}],
    )
    conn.commit()
    nid = ensure_person_node(
        conn,
        label="Jordan Lee",
        external_id="message-handle-b",
        meta={
            "imessage": "message-handle-b",
            "handle": "message-handle-b",
            "source": "messages_export",
            "evidence_refs": ["chunk:m1"],
        },
    )
    conn.commit()
    candidate = graph_candidate_list(conn)[0]

    out = apply_graph_candidate_resolution(
        conn,
        candidate["candidate_id"],
        status="approved",
        payload={"note": "same Jordan from Messages"},
        data_dir=tmp_path,
    )
    conn.commit()

    assert out["ok"] is True
    assert out["node_id"] == nid
    row = conn.execute(
        "SELECT aliases_json, summary, source_refs_json FROM graph_nodes WHERE node_id=?",
        (nid,),
    ).fetchone()
    aliases = json.loads(row["aliases_json"])
    summary = json.loads(row["summary"])
    refs = json.loads(row["source_refs_json"])
    assert "message-handle-b" in aliases
    assert summary["imessage"] == "message-handle-b"
    assert "last_merge_reasoning" in summary
    assert "same Jordan from Messages" in summary["user_note"]
    assert "chunk:m1" in refs
    assert graph_candidate_list(conn) == []


def test_life_evidence_ingest_writes_contacts_to_graph(conn, tmp_path: Path) -> None:
    evidence = tmp_path / "life_evidence"
    evidence.mkdir()
    (evidence / "contacts_latest.json").write_text(
        json.dumps(
            [
                {"display_name": "Meri Metcalf", "source": "macos_contacts"},
                {"display_name": "roshan@biktrix.com", "source": "macos_contacts"},
                {"display_name": "Ali Rashad", "source": "macos_contacts"},
            ]
        ),
        encoding="utf-8",
    )

    out = ingest_life_evidence(tmp_path, conn)
    conn.commit()

    assert out["contacts"] == 2
    rows = conn.execute(
        "SELECT title FROM graph_nodes WHERE node_kind='person' AND title IN ('Meri Metcalf', 'Ali Rashad')"
    ).fetchall()
    assert {r["title"] for r in rows} == {"Meri Metcalf", "Ali Rashad"}
