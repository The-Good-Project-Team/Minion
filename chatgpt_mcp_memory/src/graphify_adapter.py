"""Graphify shadow graph — extraction substrate; imports graph_candidates only."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from graph_fill import _is_plausible_graph_title
from store import graph_candidate_create

log = logging.getLogger(__name__)

LIFE_SHADOW = "life-shadow"
INPUT_DIR_NAME = "input"
OUTPUT_DIR_NAME = "graphify-out"
GRAPH_JSON_NAME = "graph.json"
REPORT_NAME = "GRAPH_REPORT.md"
SOURCE = "graphify_shadow"

_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
_SELF_ECHO_MARKERS = (
    "linked recent attention",
    "ambient pass linked",
    "filled so far:",
    "recently active:",
    "no contact in",
    "send?\nedit\nsnooze\ndismiss",
    "graph spine",
    "activity feed",
    "minion desktop",
)
_SKIP_BUNDLE_APPS = frozenset({"minion", "minion-desktop"})


def graphify_root(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / "graphify" / LIFE_SHADOW


def graphify_input_dir(data_dir: Path) -> Path:
    return graphify_root(data_dir) / INPUT_DIR_NAME


def graphify_output_dir(data_dir: Path) -> Path:
    return graphify_root(data_dir) / OUTPUT_DIR_NAME


def graph_json_path(data_dir: Path) -> Path:
    return graphify_output_dir(data_dir) / GRAPH_JSON_NAME


def report_path(data_dir: Path) -> Path:
    return graphify_output_dir(data_dir) / REPORT_NAME


def resolve_graphify_binary() -> Optional[str]:
    env = (os.environ.get("GRAPHIFY_BIN") or "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("graphify")
    if found:
        return found
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[1] / ".venv" / "bin" / "graphify",
        here.parents[2] / "chatgpt_mcp_memory" / ".venv" / "bin" / "graphify",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _llm_key_configured() -> bool:
    keys = (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MOONSHOT_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
    )
    return any((os.environ.get(k) or "").strip() for k in keys)


def status(data_dir: Path) -> Dict[str, Any]:
    root = graphify_root(data_dir)
    inp = graphify_input_dir(data_dir)
    out = graphify_output_dir(data_dir)
    gj = graph_json_path(data_dir)
    rp = report_path(data_dir)
    bin_path = resolve_graphify_binary()
    return {
        "graphify_installed": bool(bin_path),
        "graphify_bin": bin_path,
        "graphify_missing": not bool(bin_path),
        "llm_key_configured": _llm_key_configured(),
        "paths": {
            "root": str(root),
            "input": str(inp),
            "output": str(out),
            "graph_json": str(gj),
            "report": str(rp),
        },
        "input_bundle_ready": inp.is_dir() and any(inp.iterdir()) if inp.is_dir() else False,
        "graph_json_exists": gj.is_file(),
        "report_exists": rp.is_file(),
        "graph_json_mtime": gj.stat().st_mtime if gj.is_file() else None,
        "report_summary": read_report_summary(data_dir) if rp.is_file() else None,
    }


def _is_self_echo_text(text: str) -> bool:
    lower = (text or "").lower()
    if not lower.strip():
        return False
    if "minion" in lower and ("activity" in lower or "graph" in lower or "42" in lower):
        return True
    return any(m in lower for m in _SELF_ECHO_MARKERS)


def _sanitize_line(text: str, *, max_len: int = 400) -> str:
    line = re.sub(r"\s+", " ", (text or "").strip())
    if _is_self_echo_text(line):
        return ""
    return line[:max_len]


def _append_ref(refs: List[Dict[str, Any]], *, kind: str, ref_id: str, label: str) -> None:
    if not ref_id:
        return
    refs.append({"kind": kind, "id": ref_id, "label": label[:200]})


def build_input_bundle(conn, data_dir: Path) -> Dict[str, Any]:
    """Write curated Markdown + provenance under graphify/life-shadow/input/."""
    inp = graphify_input_dir(data_dir)
    inp.mkdir(parents=True, exist_ok=True)
    refs: List[Dict[str, Any]] = []
    excluded_echo = 0

    people_lines = ["# People", ""]
    rows = conn.execute(
        "SELECT node_id, node_kind, title, summary, aliases_json FROM graph_nodes "
        "WHERE node_kind IN ('person', 'family') AND status NOT IN ('scaffold', 'stub') "
        "ORDER BY updated_at DESC LIMIT 80"
    ).fetchall()
    for row in rows:
        title = _sanitize_line(str(row["title"] or ""))
        if not title:
            excluded_echo += 1
            continue
        if not _is_plausible_graph_title(title, node_kind=str(row["node_kind"] or "person")):
            excluded_echo += 1
            continue
        snippet = _summary_snippet(row["summary"])
        people_lines.append(f"## {title}")
        if snippet:
            people_lines.append(snippet)
        people_lines.append("")
        _append_ref(refs, kind="graph_node", ref_id=str(row["node_id"]), label=title)

    project_lines = ["# Projects", ""]
    proj_rows = conn.execute(
        "SELECT node_id, node_kind, title, summary FROM graph_nodes "
        "WHERE node_kind IN ('project', 'organization', 'role', 'job') "
        "AND status NOT IN ('scaffold', 'stub') "
        "ORDER BY updated_at DESC LIMIT 60"
    ).fetchall()
    for row in proj_rows:
        title = _sanitize_line(str(row["title"] or ""))
        if not title:
            continue
        snippet = _summary_snippet(row["summary"])
        project_lines.append(f"## {title}")
        if snippet:
            project_lines.append(snippet)
        project_lines.append("")
        _append_ref(refs, kind="graph_node", ref_id=str(row["node_id"]), label=title)

    screen_lines = ["# Screen summaries", ""]
    try:
        from screen_memory import summarize_last

        summary = summarize_last(conn, minutes=180, limit=200)
        for win in summary.get("recent_windows") or []:
            app = str(win.get("app") or "").strip().lower()
            if app in _SKIP_BUNDLE_APPS:
                continue
            window = _sanitize_line(str(win.get("window") or ""))
            if not window:
                continue
            screen_lines.append(f"- **{app or 'app'}**: {window}")
        sent = _sanitize_line(str(summary.get("summary") or ""))
        if sent:
            screen_lines.extend(["", sent])
    except Exception:
        log.debug("graphify bundle: screen summary skipped", exc_info=True)

    context_lines, context_refs, context_skipped = _indexed_context_summary(conn)
    refs.extend(context_refs)
    excluded_echo += context_skipped

    ignore_text = "\n".join(
        [
            "*.db",
            "*.sqlite",
            "*.jsonl",
            "telemetry.jsonl",
            "ambient/",
            "inbox/",
            ".env",
            "**/Minion/**",
        ]
    )

    (inp / "people.md").write_text("\n".join(people_lines).strip() + "\n", encoding="utf-8")
    (inp / "projects.md").write_text("\n".join(project_lines).strip() + "\n", encoding="utf-8")
    (inp / "screen_summaries.md").write_text("\n".join(screen_lines).strip() + "\n", encoding="utf-8")
    (inp / "context_summaries.md").write_text(
        "\n".join(context_lines).strip() + "\n", encoding="utf-8"
    )
    (inp / ".graphifyignore").write_text(ignore_text + "\n", encoding="utf-8")
    with (inp / "source_refs.jsonl").open("w", encoding="utf-8") as fh:
        for ref in refs:
            fh.write(json.dumps(ref, ensure_ascii=False) + "\n")

    return {
        "input_dir": str(inp),
        "files": sorted(p.name for p in inp.iterdir() if p.is_file()),
        "source_ref_count": len(refs),
        "excluded_echo": excluded_echo,
    }


def run_graphify_shadow(
    conn,
    data_dir: Path,
    *,
    graphify_bin: Optional[str] = None,
    skip_extract: bool = False,
) -> Dict[str, Any]:
    """Build input bundle, run `graphify extract`, import candidates."""
    bundle = build_input_bundle(conn, data_dir)
    out: Dict[str, Any] = {"bundle": bundle, "extract": None, "import": None}
    if skip_extract:
        out["import"] = import_graphify_candidates(conn, data_dir)
        return out

    bin_path = graphify_bin or resolve_graphify_binary()
    if not bin_path:
        out["extract"] = {"ok": False, "error": "graphify_missing"}
        return out

    root = graphify_root(data_dir)
    inp = graphify_input_dir(data_dir)
    if not _llm_key_configured():
        out["extract"] = {
            "ok": False,
            "error": "llm_key_missing",
            "hint": "Set GEMINI_API_KEY, OPENAI_API_KEY, or another Graphify-supported key before shadow extract.",
        }
        return out

    cmd = [bin_path, "extract", str(inp), "--out", str(root)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        out["extract"] = {"ok": False, "error": "timeout"}
        return out
    except OSError as exc:
        out["extract"] = {"ok": False, "error": str(exc)}
        return out

    extract_ok = proc.returncode == 0 and graph_json_path(data_dir).is_file()
    out["extract"] = {
        "ok": extract_ok,
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
    }
    if not extract_ok:
        if "no LLM API key" in (proc.stdout or "") + (proc.stderr or ""):
            out["extract"]["error"] = "llm_key_missing"
        elif not graph_json_path(data_dir).is_file():
            out["extract"]["error"] = out["extract"].get("error") or "graph_json_missing"
        return out

    out["import"] = import_graphify_candidates(conn, data_dir)
    return out


def reconcile_graph_truth(
    conn,
    data_dir: Path,
    *,
    graphify_bin: Optional[str] = None,
    skip_extract: bool = False,
) -> Dict[str, Any]:
    """Consistent scan path: corpus bundle -> shadow graph -> review candidates."""
    out = run_graphify_shadow(
        conn,
        data_dir,
        graphify_bin=graphify_bin,
        skip_extract=skip_extract,
    )
    out["reconcile"] = diff_shadow_to_durable(conn, data_dir)
    return out


def diff_shadow_to_durable(conn, data_dir: Path) -> Dict[str, Any]:
    """Compare shadow graph labels with durable Minion graph; never writes durable graph."""
    path = graph_json_path(data_dir)
    if not path.is_file():
        return {"ok": False, "error": "graph_json_missing"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}

    shadow_labels = {
        _normalize_label(str(n.get("label") or n.get("id") or ""))
        for n in (data.get("nodes") or [])
        if isinstance(n, dict)
    }
    shadow_labels.discard("")
    durable_rows = conn.execute(
        "SELECT node_id, node_kind, title FROM graph_nodes "
        "WHERE status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    durable = [
        {
            "node_id": str(r["node_id"]),
            "node_kind": str(r["node_kind"] or ""),
            "title": str(r["title"] or ""),
            "norm": _normalize_label(str(r["title"] or "")),
        }
        for r in durable_rows
    ]
    durable_norms = {d["norm"] for d in durable if d["norm"]}
    reinforced = sorted(durable_norms.intersection(shadow_labels))
    unrepresented = [
        {k: d[k] for k in ("node_id", "node_kind", "title")}
        for d in durable
        if d["norm"] and d["norm"] not in shadow_labels
    ]
    return {
        "ok": True,
        "shadow_nodes": len(shadow_labels),
        "durable_nodes": len(durable_norms),
        "reinforced": len(reinforced),
        "unrepresented_durable": len(unrepresented),
        "unrepresented_preview": unrepresented[:12],
        "policy": "shadow graph proposes graph_candidates; durable graph changes only by approval/hard-id policy",
    }


def read_report_summary(data_dir: Path, *, max_lines: int = 48) -> Dict[str, Any]:
    rp = report_path(data_dir)
    if not rp.is_file():
        return {"communities": [], "god_nodes": [], "headline": ""}
    text = rp.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    headline = ""
    for line in lines[:12]:
        if "nodes" in line and "edges" in line:
            headline = line.strip().strip("*")
            break

    communities: List[str] = []
    god_nodes: List[str] = []
    section = ""
    for line in lines:
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if section.startswith("communities") and line.startswith("- "):
            label = line[2:].split("—")[0].strip().strip("[]")
            if label:
                communities.append(label[:120])
        if section.startswith("god nodes") and line.startswith("- "):
            label = line[2:].split("—")[0].strip().strip("[]")
            if label:
                god_nodes.append(label[:120])

    return {
        "headline": headline,
        "communities": communities[:8],
        "god_nodes": god_nodes[:8],
        "preview_lines": lines[:max_lines],
    }


def import_graphify_candidates(
    conn,
    data_dir: Path,
    *,
    graph_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Map Graphify graph.json nodes/edges into graph_candidates (never graph_nodes)."""
    path = Path(graph_path) if graph_path else graph_json_path(data_dir)
    if not path.is_file():
        return {"created": 0, "skipped": 0, "error": "graph_json_missing"}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"created": 0, "skipped": 0, "error": str(exc)}

    nodes = [n for n in (data.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (data.get("links") or data.get("edges") or []) if isinstance(e, dict)]
    node_by_id = {str(n.get("id")): n for n in nodes if n.get("id") is not None}
    conf_by_node = _confidence_by_node(nodes, edges, node_by_id)

    existing_keys = _open_graphify_keys(conn)
    known_titles = _known_graph_titles(conn)
    created = 0
    skipped = 0

    for nid, node in node_by_id.items():
        label = _sanitize_line(str(node.get("label") or nid))
        if not label or _is_self_echo_text(label):
            skipped += 1
            continue
        kind_hint = "person" if str(node.get("file_type") or "") in ("document", "concept") else "project"
        if not _is_plausible_graph_title(label, node_kind=kind_hint):
            skipped += 1
            continue
        conf = conf_by_node.get(nid, "INFERRED")
        if conf == "AMBIGUOUS":
            ctype = "graphify_ambiguous"
            confidence = 0.25
        elif conf == "EXTRACTED":
            ctype = "graphify_node"
            confidence = 0.78 if _has_hard_identifier(label, node) else 0.65
        else:
            ctype = "graphify_node"
            confidence = 0.55

        norm = label.lower()
        if norm in known_titles:
            skipped += 1
            continue
        dedupe = f"{ctype}:{nid}"
        if dedupe in existing_keys:
            skipped += 1
            continue

        body = (
            f"Graphify shadow graph proposes **{label}** ({conf}).\n\n"
            "Review in council before it becomes durable memory."
        )
        graph_candidate_create(
            conn,
            candidate_type=ctype,
            title=f"Graphify: {label[:200]}",
            body_md=body,
            payload={
                "graphify_node_id": nid,
                "label": label,
                "confidence_tag": conf,
                "file_type": node.get("file_type"),
                "community": node.get("community"),
                "import_policy": "candidates_only",
            },
            evidence_refs=[f"graphify:{nid}"],
            confidence=confidence,
            source=SOURCE,
        )
        existing_keys.add(dedupe)
        created += 1

    edge_created = 0
    for edge in edges[:120]:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        if not src or not tgt:
            continue
        if src not in node_by_id or tgt not in node_by_id:
            skipped += 1
            continue
        conf = str(edge.get("confidence") or "INFERRED").upper()
        if conf == "AMBIGUOUS":
            continue
        rel = str(edge.get("relation") or edge.get("rel") or "related_to")[:64]
        src_label = _sanitize_line(str((node_by_id.get(src) or {}).get("label") or src))
        tgt_label = _sanitize_line(str((node_by_id.get(tgt) or {}).get("label") or tgt))
        if not src_label or not tgt_label:
            skipped += 1
            continue
        if _is_self_echo_text(src_label) or _is_self_echo_text(tgt_label):
            skipped += 1
            continue
        if not _is_plausible_graph_title(src_label) or not _is_plausible_graph_title(tgt_label):
            skipped += 1
            continue
        dedupe = f"graphify_edge:{src}:{tgt}:{rel}"
        if dedupe in existing_keys:
            skipped += 1
            continue
        title = f"Graphify: {src_label} → {tgt_label}"
        graph_candidate_create(
            conn,
            candidate_type="graphify_edge",
            title=title[:300],
            body_md=f"Proposed relation `{rel}` ({conf}).",
            payload={
                "source_node_id": src,
                "target_node_id": tgt,
                "relation": rel,
                "confidence_tag": conf,
                "import_policy": "candidates_only",
            },
            evidence_refs=[f"graphify:edge:{src}:{tgt}"],
            confidence=0.5 if conf == "INFERRED" else 0.68,
            source=SOURCE,
        )
        existing_keys.add(dedupe)
        edge_created += 1

    return {
        "created": created + edge_created,
        "nodes": created,
        "edges": edge_created,
        "skipped": skipped,
        "graph_nodes_written": 0,
    }


def graphify_spine_section(data_dir: Path) -> str:
    """Optional markdown block for build_graph_spine when shadow report exists."""
    rp = report_path(data_dir)
    if not rp.is_file():
        return ""
    summary = read_report_summary(data_dir)
    lines = ["", "### Graphify shadow"]
    if summary.get("headline"):
        lines.append(summary["headline"])
    if summary.get("god_nodes"):
        gods = ", ".join(summary["god_nodes"][:4])
        lines.append(f"Key concepts: {gods}.")
    if summary.get("communities"):
        comm = ", ".join(summary["communities"][:3])
        lines.append(f"Communities: {comm}.")
    return "\n".join(lines)


def _summary_snippet(raw: Any) -> str:
    if not raw:
        return ""
    text = str(raw)
    try:
        obj = json.loads(text) if text.startswith("{") else None
        if isinstance(obj, dict):
            text = str(obj.get("snippet") or obj.get("text") or text)
    except json.JSONDecodeError:
        pass
    return _sanitize_line(text, max_len=320)


def _indexed_context_summary(conn, *, limit_sources: int = 40) -> Tuple[List[str], List[Dict[str, Any]], int]:
    """Curated scan input from indexed sources; short excerpts only, no raw JSONL/log files."""
    rows = conn.execute(
        "SELECT s.source_id, s.path, s.kind, s.parser, s.updated_at, "
        "c.chunk_id, c.text "
        "FROM sources s JOIN chunks c ON c.source_id=s.source_id "
        "WHERE c.seq=0 AND s.kind NOT IN ('ambient', 'screen') "
        "ORDER BY s.updated_at DESC LIMIT ?",
        (int(limit_sources),),
    ).fetchall()
    lines = ["# Indexed context summaries", ""]
    refs: List[Dict[str, Any]] = []
    skipped = 0
    for row in rows:
        path = str(row["path"] or "")
        lower_path = path.lower()
        if lower_path.endswith((".jsonl", ".log", ".sqlite", ".db")):
            skipped += 1
            continue
        title = _sanitize_line(Path(path).name or path, max_len=160)
        excerpt = _sanitize_line(str(row["text"] or ""), max_len=260)
        if not title or not excerpt:
            skipped += 1
            continue
        lines.append(f"## {title}")
        lines.append(f"- kind: {row['kind']}; parser: {row['parser']}")
        lines.append(f"- excerpt: {excerpt}")
        lines.append("")
        refs.append(
            {
                "kind": "source",
                "id": str(row["source_id"]),
                "chunk_id": str(row["chunk_id"]),
                "label": title,
            }
        )
    return lines, refs, skipped


def _confidence_by_node(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    node_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, str]:
    rank = {"EXTRACTED": 3, "INFERRED": 2, "AMBIGUOUS": 1}
    out: Dict[str, str] = {}
    for edge in edges:
        conf = str(edge.get("confidence") or "INFERRED").upper()
        if conf not in rank:
            conf = "INFERRED"
        for endpoint in (edge.get("source"), edge.get("target")):
            eid = str(endpoint or "")
            if not eid:
                continue
            prev = out.get(eid, "AMBIGUOUS")
            if rank[conf] >= rank.get(prev, 0):
                out[eid] = conf
    for nid in node_by_id:
        out.setdefault(nid, "INFERRED")
    return out


def _has_hard_identifier(label: str, node: Dict[str, Any]) -> bool:
    if _EMAIL_RE.search(label):
        return True
    raw = json.dumps(node, ensure_ascii=False)
    return bool(_EMAIL_RE.search(raw))


def _open_graphify_keys(conn) -> Set[str]:
    keys: Set[str] = set()
    rows = conn.execute(
        "SELECT candidate_type, payload_json FROM graph_candidates "
        "WHERE status='open' AND source=?",
        (SOURCE,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        nid = payload.get("graphify_node_id")
        if nid:
            keys.add(f"{row['candidate_type']}:{nid}")
        src = payload.get("source_node_id")
        tgt = payload.get("target_node_id")
        rel = payload.get("relation")
        if src and tgt:
            keys.add(f"graphify_edge:{src}:{tgt}:{rel}")
    return keys


def _known_graph_titles(conn) -> Set[str]:
    rows = conn.execute(
        "SELECT title FROM graph_nodes WHERE status NOT IN ('scaffold', 'stub')"
    ).fetchall()
    return {str(r["title"] or "").strip().lower() for r in rows if r["title"]}


def _normalize_label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())

