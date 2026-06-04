"""Score one Minion version's MCP `ask_minion` path against BEIR.

This drives the PRODUCT'S MAIN OUTPUT: it calls `mcp_server._tool_ask_minion`
— the exact handler the `ask_minion` MCP tool dispatches to — which runs
_embed_query -> retrieval_engine.search_fused (dense ∥ sparse ∥ graph, RRF) ->
cross-encoder rerank -> identity rerank. Same code Claude Desktop hits.

Run once per version, pointing PYTHONPATH at that version's src and
MINION_DATA_DIR at the DB it already ingested:

  MINION_DATA_DIR=/tmp/m1-data MINION_EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2 \
    PYTHONPATH=/tmp/minion1-app/chatgpt_mcp_memory/src \
    chatgpt_mcp_memory/.venv/bin/python eval/ask_bench.py scifact --label "Minion 1"

Prints one JSON line with the metrics so a caller can diff two runs.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beir_eval import _load, _mrr_at_k, _ndcg_at_k, _recall_at_k  # noqa: E402

K = 10


def _stem(path: str) -> str:
    base = str(path or "").split("/")[-1].split("#", 1)[0]
    return base.rsplit(".", 1)[0] if "." in base else base


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", nargs="?", default="scifact")
    ap.add_argument("--label", default="minion")
    ap.add_argument("--top-k", type=int, default=40)
    args = ap.parse_args()

    import mcp_server  # the version under test (from PYTHONPATH)

    _corpus, queries, qrels = _load(args.dataset)
    print(f"[{args.label}] {len(queries)} queries via mcp_server._tool_ask_minion "
          f"(rerank={getattr(__import__('retrieval_engine'), 'rerank_enabled', lambda: '?')()})",
          file=sys.stderr, flush=True)

    agg = {"ndcg": [], "recall": [], "mrr": []}
    lat: list = []
    for i, (qid, q) in enumerate(queries.items()):
        t0 = time.perf_counter()
        try:
            out = mcp_server._tool_ask_minion({"query": q, "top_k": args.top_k, "mode": "relevance"})
        except Exception as exc:  # noqa: BLE001
            print(f"[{args.label}] query {qid} failed: {exc}", file=sys.stderr, flush=True)
            continue
        lat.append((time.perf_counter() - t0) * 1000)
        ranked, seen = [], set()
        for ch in out.get("chunks", []):
            d = _stem(ch.get("path", ""))
            if d and d not in seen:
                seen.add(d)
                ranked.append(d)
            if len(ranked) >= K:
                break
        rel = qrels[qid]
        agg["ndcg"].append(_ndcg_at_k(ranked, rel, K))
        agg["recall"].append(_recall_at_k(ranked, rel, K))
        agg["mrr"].append(_mrr_at_k(ranked, rel, K))
        if (i + 1) % 50 == 0:
            print(f"[{args.label}] {i+1}/{len(queries)}", file=sys.stderr, flush=True)

    res = {
        "label": args.label,
        "dataset": args.dataset,
        "n": len(agg["ndcg"]),
        "ndcg@10": sum(agg["ndcg"]) / len(agg["ndcg"]) if agg["ndcg"] else 0.0,
        "recall@10": sum(agg["recall"]) / len(agg["recall"]) if agg["recall"] else 0.0,
        "mrr@10": sum(agg["mrr"]) / len(agg["mrr"]) if agg["mrr"] else 0.0,
        "latency_ms_p50": sorted(lat)[len(lat) // 2] if lat else 0.0,
        "latency_ms_mean": sum(lat) / len(lat) if lat else 0.0,
    }
    print(json.dumps(res))


if __name__ == "__main__":
    main()
