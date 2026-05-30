from pathlib import Path

from forty_two import stream_reply
from identity_companion import companion_overview, open_companion_thread
from store import connect, identity_claim_list, seed_sync_sources


def test_companion_overview_has_world_pillars(tmp_path: Path) -> None:
    conn = connect(tmp_path / "memory.db")
    try:
        seed_sync_sources(conn)
        overview = companion_overview(conn, tmp_path)
    finally:
        conn.close()

    assert overview["tagline"]
    assert overview["readiness"] >= 0
    assert {p["label"] for p in overview["pillars"]} >= {"People", "Projects", "Obligations", "Preferences"}
    assert overview["starter_prompts"]


def test_companion_thread_records_chat_as_identity_signal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "memory.db")
    try:
        seed_sync_sources(conn)
        out = open_companion_thread(conn)
        tid = out["thread"]["thread_id"]
        events = list(stream_reply(conn, tid, body="I like direct plans and visible open loops.", data_dir=tmp_path))
        claims = identity_claim_list(conn, status="active", kind="preference", limit=20)
    finally:
        conn.close()

    assert out["created"] is True
    assert any(e["event"] == "message.assistant.done" for e in events)
    assert any("visible open loops" in c["text"] for c in claims)


def test_identity_companion_http(sidecar) -> None:
    r = sidecar.get("/identity/companion")
    assert r.status_code == 200, r.text
    assert r.json()["pillars"]

    started = sidecar.post("/identity/companion/start", {})
    assert started.status_code == 200, started.text
    assert started.json()["thread"]["thread_id"]
