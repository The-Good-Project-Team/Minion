from pathlib import Path

import pytest

from consent_policy import privacy_matrix, reader_allowed_strata
from context_platform import CONTEXT_BUNDLE_SCHEMA_VERSION, enrich_context_bundle
from store import connect, seed_sync_sources


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


def test_enrich_context_bundle(conn, tmp_path: Path) -> None:
    base = {"subject": "", "context_md": "test"}
    out = enrich_context_bundle(conn, tmp_path, base, reader_id="mcp")
    assert out["schema_version"] == CONTEXT_BUNDLE_SCHEMA_VERSION
    assert out["privacy_scope"]["reader"] == "mcp"
    assert "preferences" in out
