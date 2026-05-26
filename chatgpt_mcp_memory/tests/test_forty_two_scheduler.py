from pathlib import Path
from unittest.mock import patch

import pytest

from forty_two_scheduler import tick
from store import connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_tick_opens_thread_when_gemini_and_gap(conn, tmp_path: Path) -> None:
    sec = tmp_path / ".secrets"
    sec.mkdir()
    (sec / "gemini_api_key").write_text("fake", encoding="utf-8")
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    fake_tid = "thr-scheduler-test"
    with (
        patch("forty_two_scheduler._notify_chat_updated"),
        patch(
            "graph_fill.open_thread_for_gap",
            return_value={
                "thread": {"thread_id": fake_tid},
                "created": True,
                "gap": {"gap_type": "bucket"},
            },
        ),
    ):
        tid = tick(conn, tmp_path)
    assert tid == fake_tid
