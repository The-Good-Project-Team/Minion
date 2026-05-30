from pathlib import Path

import pytest

from consent_policy import filter_hits_for_mcp, privacy_matrix, reader_allowed_strata
from context_platform import CONTEXT_BUNDLE_SCHEMA_VERSION, enrich_context_bundle
from store import Hit, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_privacy_matrix_readers() -> None:
    m = privacy_matrix()
    assert "mcp" in m["readers"]
    assert "raw_evidence" not in m["readers"]["mcp"]["allowed_strata"]


def test_reader_allowed_strata_defaults(tmp_path: Path) -> None:
    local = reader_allowed_strata("local_ui", tmp_path)
    mcp = reader_allowed_strata("mcp", tmp_path)
    assert "raw_evidence" in local
    assert "raw_evidence" not in mcp
    assert "work_context" in mcp


def test_mcp_release_request_before_level_three_context(tmp_path: Path) -> None:
    hit = Hit(
        chunk_id="screen-1",
        score=0.9,
        text="User is debugging The Good Project investor update.",
        role="ambient",
        source_id="src-screen-1",
        path="ambient/screen-events/today/screen-1.md",
        kind="screen-event",
        mtime=1.0,
        meta={},
        source_meta={},
    )

    filtered = filter_hits_for_mcp([hit], tmp_path)
    assert filtered[0].kind == "release-request"
    assert filtered[0].meta["release_level"] == 3
    assert "The Good Project" not in filtered[0].text

    approved = filter_hits_for_mcp(
        [hit],
        tmp_path,
        release_ok=True,
        approved_release_level=3,
    )
    assert approved[0].kind == "screen-event"
    assert "The Good Project" in approved[0].text


def test_enrich_context_bundle(conn, tmp_path: Path) -> None:
    base = {"subject": "", "context_md": "test"}
    out = enrich_context_bundle(conn, tmp_path, base, reader_id="mcp")
    assert out["schema_version"] == CONTEXT_BUNDLE_SCHEMA_VERSION
    assert out["privacy_scope"]["reader"] == "mcp"
    assert "preferences" in out
