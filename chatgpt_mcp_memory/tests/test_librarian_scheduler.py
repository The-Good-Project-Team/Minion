from pathlib import Path

import pytest

from librarian_scheduler import tick
from store import connect, seed_sync_sources


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "memory.db"
    c = connect(db)
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_tick_opens_thread_when_gemini_and_gap(conn, tmp_path: Path, fake_gemini) -> None:
    fake_gemini('{"confidence":0.8,"actions":[],"unresolved_question":""}')
    conn.execute(
        "DELETE FROM graph_nodes WHERE node_kind='person' AND status NOT IN ('scaffold', 'stub')"
    )
    conn.commit()
    tid = tick(conn, tmp_path)
    assert tid
    row = conn.execute("SELECT thread_id FROM chat_threads WHERE thread_id=?", (tid,)).fetchone()
    assert row is not None
