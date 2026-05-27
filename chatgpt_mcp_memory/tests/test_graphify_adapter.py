"""Graphify shadow adapter — bundle hygiene, candidate-only import, status."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from graphify_adapter import (
    build_input_bundle,
    graph_json_path,
    graphify_output_dir,
    import_graphify_candidates,
    reconcile_graph_truth,
    read_report_summary,
    report_path,
    resolve_graphify_binary,
    run_graphify_shadow,
    status,
)
from graph_fill import _create_graph_node
from store import (
    connect,
    graph_candidate_list,
    seed_sync_sources,
)


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "memory.db")
    seed_sync_sources(c)
    c.commit()
    yield c
    c.close()


def test_status_reports_missing_graphify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAPHIFY_BIN", raising=False)
    monkeypatch.setattr("graphify_adapter.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "graphify_adapter.resolve_graphify_binary",
        lambda: None,
    )
    out = status(tmp_path)
    assert out["graphify_missing"] is True
    assert out["graphify_installed"] is False


def test_build_input_bundle_excludes_self_echo(conn, tmp_path: Path) -> None:
    _create_graph_node(
        conn,
        "scaffold-people-unknown",
        "person",
        "Linked recent attention to reiftauati",
    )
    _create_graph_node(
        conn,
        "scaffold-people-friends",
        "person",
        "Alex Kim",
        user_note="Engineer at Acme.",
    )
    conn.commit()
    bundle = build_input_bundle(conn, tmp_path)
    people = (tmp_path / "graphify" / "life-shadow" / "input" / "people.md").read_text(encoding="utf-8")
    assert "Alex Kim" in people
    assert "reiftauati" not in people.lower()
    assert bundle["source_ref_count"] >= 1


def test_build_input_bundle_includes_curated_indexed_context(conn, tmp_path: Path) -> None:
    now = 1_700_000_000.0
    conn.execute(
        "INSERT INTO sources(source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at) "
        "VALUES('src-context', '/vault/notes/strategy.md', 'text', 'abc', ?, 120, 'markdown', '{}', ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO chunks(chunk_id, source_id, seq, role, text, meta_json) "
        "VALUES('chk-context', 'src-context', 0, 'user', ?, '{}')",
        ("Jordan Lee now leads the Atlas project and should be followed up with weekly.",),
    )
    conn.execute(
        "INSERT INTO sources(source_id, path, kind, sha256, mtime, bytes, parser, meta_json, updated_at) "
        "VALUES('src-log', '/vault/ambient/stream.jsonl', 'text', 'def', ?, 120, 'jsonl', '{}', ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO chunks(chunk_id, source_id, seq, role, text, meta_json) "
        "VALUES('chk-log', 'src-log', 0, 'user', 'raw ambient log text', '{}')"
    )
    conn.commit()

    bundle = build_input_bundle(conn, tmp_path)
    context = (tmp_path / "graphify" / "life-shadow" / "input" / "context_summaries.md").read_text(
        encoding="utf-8"
    )

    assert "strategy.md" in context
    assert "Atlas project" in context
    assert "stream.jsonl" not in context
    assert bundle["source_ref_count"] >= 1


def test_import_graphify_candidates_only(conn, tmp_path: Path) -> None:
    out_dir = graphify_output_dir(tmp_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = {
        "nodes": [
            {"id": "n1", "label": "Jordan Lee", "file_type": "document"},
            {"id": "n2", "label": "Openai Codex:reiftauati", "file_type": "document"},
        ],
        "links": [
            {
                "source": "n1",
                "target": "n2",
                "relation": "knows",
                "confidence": "EXTRACTED",
            },
            {
                "source": "n1",
                "target": "n3",
                "relation": "works_on",
                "confidence": "INFERRED",
            },
        ],
    }
    graph_json_path(tmp_path).write_text(json.dumps(graph), encoding="utf-8")
    (report_path(tmp_path)).write_text(
        "# Graph Report\n\n**3 nodes · 2 edges · 1 communities**\n\n## God Nodes\n- [[Jordan Lee]] — 2 connections\n",
        encoding="utf-8",
    )

    before_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    result = import_graphify_candidates(conn, tmp_path)
    conn.commit()
    after_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]

    assert result["created"] >= 1
    assert result["graph_nodes_written"] == 0
    assert after_nodes == before_nodes
    open_rows = graph_candidate_list(conn, status="open")
    titles = [r["title"] for r in open_rows]
    assert any("Jordan" in t for t in titles)
    assert not any("reiftauati" in t.lower() for t in titles)
    assert all(r["source"] == "graphify_shadow" for r in open_rows if "Graphify" in r["title"])


def test_import_skips_existing_graph_title(conn, tmp_path: Path) -> None:
    _create_graph_node(conn, "scaffold-people-friends", "person", "Jordan Lee")
    conn.commit()
    out_dir = graphify_output_dir(tmp_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph_json_path(tmp_path).write_text(
        json.dumps({"nodes": [{"id": "n1", "label": "Jordan Lee"}], "links": []}),
        encoding="utf-8",
    )
    result = import_graphify_candidates(conn, tmp_path)
    assert result["created"] == 0
    assert result["skipped"] >= 1


def test_read_report_summary_parses_god_nodes(tmp_path: Path) -> None:
    report_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    report_path(tmp_path).write_text(
        "# Report\n\n**10 nodes · 5 edges · 2 communities**\n\n"
        "## Communities\n- [[Work]] — 4 nodes\n\n## God Nodes\n- [[Minion]] — 9 connections\n",
        encoding="utf-8",
    )
    summary = read_report_summary(tmp_path)
    assert "10 nodes" in summary["headline"]
    assert "Work" in summary["communities"][0]
    assert summary["god_nodes"][0] == "Minion"


def test_reconcile_graph_truth_import_only_reports_shadow_vs_durable(conn, tmp_path: Path) -> None:
    _create_graph_node(conn, "scaffold-people-friends", "person", "Jordan Lee")
    conn.commit()
    graphify_output_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    graph_json_path(tmp_path).write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "n1", "label": "Jordan Lee", "file_type": "document"},
                    {"id": "n2", "label": "Atlas Project", "file_type": "document"},
                ],
                "links": [],
            }
        ),
        encoding="utf-8",
    )

    before_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    out = reconcile_graph_truth(conn, tmp_path, skip_extract=True)
    after_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]

    assert out["reconcile"]["ok"] is True
    assert out["reconcile"]["reinforced"] == 1
    assert out["import"]["created"] == 1
    assert after_nodes == before_nodes


def test_run_graphify_shadow_with_fake_binary(
    conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake-graphify"
    fake.write_text(
        "#!/bin/sh\n"
        'OUT="$4/graphify-out"\n'
        'mkdir -p "$OUT"\n'
        'cat > "$OUT/graph.json" <<\'EOF\'\n'
        '{"nodes":[{"id":"p1","label":"Casey Morgan","file_type":"document"}],'
        '"links":[{"source":"p1","target":"p2","relation":"knows","confidence":"INFERRED"}]}\n'
        "EOF\n"
        'echo "# Graph Report" > "$OUT/GRAPH_REPORT.md"\n'
        'echo "**1 nodes · 1 edges · 0 communities**" >> "$OUT/GRAPH_REPORT.md"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    out = run_graphify_shadow(conn, tmp_path, graphify_bin=str(fake))
    conn.commit()

    assert out["extract"]["ok"] is True
    assert out["import"]["created"] >= 1
    assert graph_candidate_list(conn, status="open")


def test_resolve_graphify_binary_prefers_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "my-graphify"
    custom.write_text("#!/bin/sh\n", encoding="utf-8")
    custom.chmod(custom.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("GRAPHIFY_BIN", str(custom))
    assert resolve_graphify_binary() == str(custom)

