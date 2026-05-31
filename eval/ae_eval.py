#!/usr/bin/env python3
"""Agent-Experience (AE) eval harness for minion's MCP memory.

An LLM AGENT (Gemini) is given a question and must answer it by calling minion's
MCP tools in-process (via ``mcp_server._DISPATCH``). It runs a bounded tool-use
loop (default cap 5 tool calls), then an LLM JUDGE (Gemini) scores the final
answer on the two metrics chosen by the user:

  1. CORRECTNESS  — does the answer match the ground truth? (0..1)
  2. RECALL       — did it surface ALL relevant memories, not just one?
                    Computed as the fraction of the task's ``must_recall`` items
                    the answer covers (0..1).

We also record TOOL-CALL COUNT per task as an efficiency signal, and print a
per-task + aggregate summary table.

Design notes
------------
* Both the agent and the judge reuse ``gemini_client.gemini_chat`` (minion's own
  stdlib Gemini REST wrapper), so the whole thing runs in the PROJECT venv with
  only ``GEMINI_API_KEY`` set — no ragas/deepeval venv juggling.
* The harness logic is corpus-agnostic. All corpus references live in the task
  file (``eval/ae_tasks.yaml`` by default).
* ``--config`` (or ``AE_CONFIG`` env) varies the retrieval configuration so two
  setups can be compared later. Today both map to the same index-pointer tools;
  the flag controls the agent's default ``ask_minion`` ``mode`` and ``top_k`` so
  a baseline (e.g. ``keyword``) can be diffed against the pointer model
  (``relevance``).

Run (from chatgpt_mcp_memory/):
    MINION_DATA_DIR=/tmp/minion-runtime \
    GEMINI_API_KEY=$(head -1 ../gemini_key.md) \
    PYTHONPATH=src .venv/bin/python ../eval/ae_eval.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- make minion's src importable even if PYTHONPATH wasn't set ---------------
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "chatgpt_mcp_memory" / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import yaml  # noqa: E402  (yaml ships with the project venv)

import mcp_server  # noqa: E402
from gemini_client import gemini_chat  # noqa: E402

DEFAULT_TASKS = _HERE / "ae_tasks.yaml"
MAX_TOOL_CALLS = 5

# gemini-2.5-pro spends most of a small token budget on hidden "thinking" and
# returns empty under response_mime_type=json. Flash is fast, cheap, and emits
# the JSON we asked for, so we use it for both agent + judge by default. Override
# with AE_GEMINI_MODEL.
AE_MODEL = os.environ.get("AE_GEMINI_MODEL", "gemini-2.5-flash")

# Tools the agent is allowed to call. Maps the agent-facing name to the
# mcp_server dispatch entry. These are the index-pointer model's read tools.
AGENT_TOOLS = {
    "ask_minion": "ask_minion",
    "get_chunk": "get_chunk",
    "get_node": "get_node",
    "conversation_chunks": "conversation_chunks",
}

# Retrieval configurations to compare. Each tweaks the agent's default
# ask_minion call so a baseline can be diffed against the pointer model.
CONFIGS = {
    "pointer": {  # the new index-pointer model: dense+graph fused relevance
        "default_mode": "relevance",
        "default_top_k": 6,
        "label": "index-pointer (relevance)",
    },
    "baseline": {  # a weaker baseline: plain keyword search, smaller k
        "default_mode": "keyword",
        "default_top_k": 6,
        "label": "baseline (keyword)",
    },
}


# ---------------------------------------------------------------------------
# MCP tool invocation (in-process)
# ---------------------------------------------------------------------------
def call_tool(name: str, arguments: Dict[str, Any]) -> Any:
    """Invoke an MCP tool in-process and return its raw result dict/list."""
    handler = mcp_server._DISPATCH.get(AGENT_TOOLS.get(name, ""))
    if handler is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return handler(arguments or {})
    except Exception as e:  # surface errors to the agent like the server does
        return {"error": str(e)}


def _trim_tool_result(name: str, result: Any, *, max_chars: int = 2200) -> Any:
    """Shrink a tool result so the agent transcript stays token-disciplined."""
    if isinstance(result, dict) and "error" in result:
        return result
    if name == "ask_minion" and isinstance(result, dict):
        chunks = []
        for c in (result.get("chunks") or [])[:8]:
            chunks.append(
                {
                    "chunk_id": c.get("chunk_id"),
                    "score": c.get("score"),
                    "kind": c.get("kind"),
                    "path": (c.get("path") or "").split("/")[-1],
                    "conversation_id": c.get("conversation_id"),
                    "text": (c.get("text") or "")[:400],
                }
            )
        graph = []
        for g in (result.get("graph") or [])[:6]:
            graph.append(
                {
                    "node_id": g.get("node_id"),
                    "label": g.get("label"),
                    "kind": g.get("kind"),
                    "fact": (g.get("fact") or "")[:300],
                }
            )
        return {"chunks": chunks, "graph": graph, "expand": result.get("expand")}
    # generic truncation for get_chunk / get_node / conversation_chunks
    blob = json.dumps(result, default=str)
    if len(blob) > max_chars:
        return {"_truncated": True, "preview": blob[:max_chars]}
    return result


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
AGENT_SYSTEM = """You are a research agent answering a question using ONLY a \
personal-memory search system (minion's MCP tools). You cannot use outside \
knowledge; everything must come from tool results.

Available tools:
- ask_minion(query, mode, top_k): search memory. Returns an INDEX: `chunks` \
(pointers with chunk_id + a text preview), `graph` (entity pointers with \
node_id + a fact), and `expand.conversation_ids`. This is a map, not the full \
content.
- get_chunk(chunk_id): full text of one chunk pointer.
- get_node(node_id): expand a graph entity into its summary + connected nodes \
(edges). Use this to learn what an entity is RELATED to.
- conversation_chunks(conversation_id): all chunks of one conversation.

Strategy: search first, then expand the most relevant pointers. To answer \
COMPLETELY, gather ALL relevant memories, not just the first hit. If the memory \
genuinely contains nothing about the question, say so honestly rather than \
guessing.

You have a hard budget of {budget} tool calls. On each turn respond with STRICT \
JSON and nothing else:
  - to call a tool: {{"action":"tool","tool":"<name>","arguments":{{...}}}}
  - to finish:      {{"action":"final","answer":"<your answer>"}}
Call a tool only when it will add information; finish as soon as you can answer.
"""

# NB: we deliberately do NOT pass a response_schema for the agent turn. Gemini's
# constrained decoding of a free-string `tool` field degenerates into runaway
# token repetition, and typing `arguments` as a bare object makes it drop the
# nested `query`. response_mime_type=application/json + a strong prompt + the
# defensive parser below is reliable.


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        # last resort: grab the first {...} block
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j > i:
            try:
                return json.loads(text[i : j + 1])
            except Exception:
                return None
    return None


def _unwrap_answer(decision: Dict[str, Any], raw: str) -> str:
    """Extract the final answer, unwrapping a nested JSON envelope if the model
    put another {"action":"final","answer":...} inside the answer string."""
    ans = str(decision.get("answer") or raw).strip()
    for _ in range(3):
        inner = _parse_json(ans)
        if isinstance(inner, dict) and "answer" in inner:
            ans = str(inner.get("answer") or "").strip()
        else:
            break
    return ans


def run_agent(question: str, config: Dict[str, Any], *, verbose: bool = False) -> Dict[str, Any]:
    """Run the bounded tool-use loop. Returns answer + trace + tool counts."""
    system = AGENT_SYSTEM.format(budget=MAX_TOOL_CALLS)
    messages: List[Dict[str, str]] = [
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Default search hint: prefer ask_minion(mode='{config['default_mode']}', "
                f"top_k={config['default_top_k']}) for the first search. Begin."
            ),
        }
    ]
    tool_calls: List[Dict[str, Any]] = []
    tools_used: List[str] = []
    final_answer = ""

    for step in range(MAX_TOOL_CALLS + 1):
        # On the last allowed step, force a final answer.
        force_final = step == MAX_TOOL_CALLS
        prompt_messages = list(messages)
        if force_final:
            prompt_messages.append(
                {
                    "role": "user",
                    "content": "Tool-call budget exhausted. Respond now with "
                    '{"action":"final","answer":...} using what you have.',
                }
            )
        try:
            raw = gemini_chat(
                system=system,
                messages=prompt_messages,
                model=AE_MODEL,
                temperature=0.1,
                max_output_tokens=2048,
                response_mime_type="application/json",
            )
        except Exception as e:
            final_answer = f"[agent error: {e}]"
            break

        decision = _parse_json(raw) or {}
        action = str(decision.get("action") or "").lower()

        if action == "final" or force_final or not action:
            final_answer = _unwrap_answer(decision, raw)
            break

        if action == "tool":
            tool = str(decision.get("tool") or "")
            args = decision.get("arguments") or {}
            if tool == "ask_minion":
                args.setdefault("mode", config["default_mode"])
                args.setdefault("top_k", config["default_top_k"])
                # Guard against the agent omitting the query: relevance/keyword
                # modes require one, so fall back to the question text rather
                # than wasting a tool call on a "query is required" error.
                if not str(args.get("query") or "").strip():
                    args["query"] = question
            result = call_tool(tool, args)
            trimmed = _trim_tool_result(tool, result)
            tool_calls.append({"tool": tool, "arguments": args})
            tools_used.append(tool)
            if verbose:
                print(f"    -> {tool}({json.dumps(args, default=str)[:120]})")
            # feed the decision + observation back into the conversation
            messages.append({"role": "assistant", "content": json.dumps(decision, default=str)})
            messages.append(
                {
                    "role": "user",
                    "content": "TOOL RESULT for "
                    f"{tool}:\n{json.dumps(trimmed, default=str)[:3000]}",
                }
            )
            continue

        # unrecognized action -> treat raw as the answer
        final_answer = _unwrap_answer(decision, raw)
        break

    return {
        "answer": final_answer,
        "tool_calls": tool_calls,
        "tools_used": tools_used,
        "n_tool_calls": len(tool_calls),
    }


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------
JUDGE_SYSTEM = """You are a strict evaluator of an AI memory assistant's answer. \
You are given a question, a ground-truth ideal answer, a checklist of facts a \
COMPLETE answer must surface, and the assistant's actual answer.

Score two metrics, each as a float in [0,1]:
- correctness: how factually consistent the answer is with the ideal answer. \
1.0 = fully correct and faithful; 0.0 = wrong or hallucinated. Penalize claims \
not supported by the ideal answer.
- recall: the fraction of the checklist items the answer actually surfaces. If \
the answer covers 2 of 4 checklist items, recall is ~0.5. Reward completeness; \
do not reward only finding one relevant fact when several were expected.

For an absence/negative task (the ideal answer says the info is NOT in memory), \
an honest 'not found / not in memory' answer should score HIGH on both metrics, \
and a confident fabricated answer should score 0.

Also list which checklist items were covered. Respond with STRICT JSON only.
"""

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "correctness": {"type": "number"},
        "recall": {"type": "number"},
        "covered_items": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["correctness", "recall", "rationale"],
}


def judge_answer(task: Dict[str, Any], answer: str) -> Dict[str, Any]:
    checklist = task.get("must_recall") or []
    payload = {
        "question": task.get("question"),
        "ideal_answer": " ".join(str(task.get("ideal") or "").split()),
        "completeness_checklist": checklist,
        "judge_note": " ".join(str(task.get("note") or "").split()) or None,
        "assistant_answer": answer,
    }
    try:
        raw = gemini_chat(
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            model=AE_MODEL,
            temperature=0.0,
            max_output_tokens=1024,
            response_mime_type="application/json",
            response_schema=_JUDGE_SCHEMA,
        )
    except Exception as e:
        return {"correctness": 0.0, "recall": 0.0, "rationale": f"[judge error: {e}]",
                "covered_items": []}
    parsed = _parse_json(raw) or {}

    def _clamp(x: Any) -> float:
        try:
            return max(0.0, min(1.0, float(x)))
        except Exception:
            return 0.0

    return {
        "correctness": _clamp(parsed.get("correctness")),
        "recall": _clamp(parsed.get("recall")),
        "covered_items": parsed.get("covered_items") or [],
        "rationale": str(parsed.get("rationale") or "")[:400],
    }


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def load_tasks(path: Path) -> List[Dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    tasks = data.get("tasks") if isinstance(data, dict) else data
    return list(tasks or [])


def _fmt(x: float) -> str:
    return f"{x:.2f}"


def print_table(rows: List[Dict[str, Any]], config_label: str) -> None:
    print()
    print(f"AGENT-EXPERIENCE EVAL  —  config: {config_label}")
    print("=" * 78)
    header = f"{'task':22} {'correct':>8} {'recall':>7} {'tools':>6}  tools_used"
    print(header)
    print("-" * 78)
    for r in rows:
        used = ",".join(r["tools_used"]) or "-"
        print(
            f"{r['id'][:22]:22} {_fmt(r['correctness']):>8} {_fmt(r['recall']):>7} "
            f"{r['n_tool_calls']:>6}  {used[:30]}"
        )
    print("-" * 78)
    n = len(rows) or 1
    agg_c = sum(r["correctness"] for r in rows) / n
    agg_r = sum(r["recall"] for r in rows) / n
    agg_t = sum(r["n_tool_calls"] for r in rows) / n
    print(
        f"{'AGGREGATE (mean)':22} {_fmt(agg_c):>8} {_fmt(agg_r):>7} {agg_t:>6.1f}"
    )
    print("=" * 78)
    print(
        f"correctness={_fmt(agg_c)}  recall={_fmt(agg_r)}  "
        f"avg_tool_calls={agg_t:.2f}  tasks={len(rows)}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent-Experience eval for minion MCP")
    ap.add_argument("--tasks", default=str(DEFAULT_TASKS), help="path to ae_tasks.yaml")
    ap.add_argument(
        "--config",
        default=os.environ.get("AE_CONFIG", "pointer"),
        choices=sorted(CONFIGS),
        help="retrieval configuration to evaluate",
    )
    ap.add_argument("--limit", type=int, default=0, help="only run the first N tasks")
    ap.add_argument("--verbose", action="store_true", help="print agent tool calls")
    ap.add_argument("--json-out", default="", help="write per-task results JSON here")
    args = ap.parse_args()

    config = CONFIGS[args.config]
    tasks = load_tasks(Path(args.tasks))
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("no tasks loaded", file=sys.stderr)
        return 2

    print(f"Loaded {len(tasks)} tasks from {args.tasks}")
    print(f"Config: {args.config} -> {config['label']}")
    rows: List[Dict[str, Any]] = []
    for i, task in enumerate(tasks, 1):
        tid = task.get("id") or f"task{i}"
        print(f"\n[{i}/{len(tasks)}] {tid}: {task.get('question')}")
        t0 = time.time()
        agent = run_agent(task["question"], config, verbose=args.verbose)
        verdict = judge_answer(task, agent["answer"])
        dt = time.time() - t0
        print(f"    answer: {agent['answer'][:160].replace(chr(10), ' ')}")
        print(
            f"    correctness={_fmt(verdict['correctness'])} "
            f"recall={_fmt(verdict['recall'])} tools={agent['n_tool_calls']} "
            f"({dt:.1f}s)"
        )
        rows.append(
            {
                "id": tid,
                "question": task.get("question"),
                "answer": agent["answer"],
                "tools_used": agent["tools_used"],
                "n_tool_calls": agent["n_tool_calls"],
                "correctness": verdict["correctness"],
                "recall": verdict["recall"],
                "covered_items": verdict.get("covered_items"),
                "rationale": verdict.get("rationale"),
                "seconds": round(dt, 1),
            }
        )

    print_table(rows, config["label"])

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"config": args.config, "results": rows}, indent=2, default=str)
        )
        print(f"\nWrote per-task JSON to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
