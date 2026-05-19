"""Ambient hourly consolidation into ambient-summary chunks."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ambient_consolidation import run_ambient_consolidation
from store import ambient_event_insert_ignore, connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c, tmp_path
    c.close()


def test_consolidation_writes_summary(conn) -> None:
    c, data_dir = conn
    now = time.time()
    for i in range(3):
        ambient_event_insert_ignore(
            c,
            event_type="window_focus",
            captured_at=now - 60 + i,
            dedupe_key=f"wf:{i}",
            payload={
                "app_name": "Chrome",
                "window_title": f"Tab {i}",
                "url": "https://example.com",
            },
            sensitivity="vault_local",
            storage_tier="hot",
        )
    c.commit()
    out = run_ambient_consolidation(c, data_dir, force=True)
    assert out.get("skipped") is False
    assert out.get("summaries_written", 0) >= 1
    row = c.execute(
        "SELECT kind FROM sources WHERE kind='ambient-summary' LIMIT 1"
    ).fetchone()
    assert row is not None


def test_deny_blocks_ingest(conn) -> None:
    from ambient_pipeline import ingest_ambient_jsonl

    c, data_dir = conn
    settings_path = data_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "ambient_sensing_enabled": True,
                "ambient_deny": {
                    "app_names": ["1Password"],
                    "title_substrings": [],
                },
            }
        ),
        encoding="utf-8",
    )
    stream = data_dir / "ambient" / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "kind": "window_focus",
                "app_name": "1Password",
                "window_title": "Vault",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = ingest_ambient_jsonl(data_dir=data_dir, conn=c)
    c.commit()
    assert out["ingested"] == 0
