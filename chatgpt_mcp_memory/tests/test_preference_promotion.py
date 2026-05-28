from pathlib import Path

from preference_promotion import record_display_name, record_explicit_preference, record_council_feedback
from store import connect, identity_claim_list, seed_sync_sources


import pytest


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_record_explicit_preference_proposed(conn) -> None:
    out = record_explicit_preference(
        conn,
        text="I prefer concise bullet replies",
        source="test",
    )
    assert out["ok"] is True
    assert out["claim"] is not None
    assert out["claim"].get("kind") == "preference"
    assert out["claim"].get("status") == "proposed"


def test_record_explicit_preference_active(conn) -> None:
    out = record_explicit_preference(
        conn,
        text="Always use my first name in greetings",
        source="onboarding",
        auto_activate=True,
    )
    assert out["ok"] is True
    assert out.get("activated") is True
    active = [c for c in identity_claim_list(conn, status="active") if c["kind"] == "preference"]
    assert len(active) >= 1


def test_record_display_name_active(conn) -> None:
    out = record_display_name(conn, display_name="Journey User", source="onboarding")
    assert out["ok"] is True
    assert out.get("activated") is True
    active = [c for c in identity_claim_list(conn, status="active") if c["kind"] == "preference"]
    assert any("Journey User" in (c.get("text") or "") for c in active)


def test_record_council_feedback(conn) -> None:
    out = record_council_feedback(
        conn,
        proposal_type="outbound_message",
        action="reject",
        title="Email mom",
        summary="Weekly check-in",
    )
    assert out["ok"] is True
