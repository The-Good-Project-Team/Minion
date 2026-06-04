"""Isolate the CHUNKING penalty in minion retrieval, fast.

Dense-only (no rerank/fusion/graph), bge-768, scored doc-level (best chunk per
doc) on BEIR — varying ONLY the chunk size, using minion's real chunker:

  whole    : embed the whole doc (no split)            -> the model's ceiling
  chunk1200: parsers._common.chunk_text(max_chars=1200) -> current behavior
  chunk2000: chunk_text(max_chars=2000)                 -> ~bge's 512-tok budget

If "whole"/"chunk2000" >> "chunk1200", chunking at 1200 is the bottleneck.

  cd chatgpt_mcp_memory && PYTHONPATH=src .venv/bin/python ../eval/chunk_probe.py scifact
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beir_eval import _load, _mrr_at_k, _ndcg_at_k, _recall_at_k  # noqa: E402

K = 10
BGE = "BAAI/bge-base-en-v1.5"
QPREFIX = "Represent this sentence for searching relevant passages: "


def _norm(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def _build(corpus: dict, embed, mode: str):
    """Return (chunk_matrix [N,d], chunk_owner_docid [N])."""
    from parsers._common import chunk_text

    texts, owners = [], []
    for cid, t in corpus.items():
        if mode == "whole":
            pieces = [t]
        else:
            mc = 1200 if mode == "chunk1200" else 2000
            pieces = chunk_text(t, max_chars=mc) or [t]
        for p in pieces:
            texts.append(p)
            owners.append(cid)
    t0 = time.perf_counter()
    vecs = _norm(np.array(list(embed.embed(texts)), dtype=np.float32))
    print(f"  [{mode}] {len(texts)} chunks embedded in {time.perf_counter()-t0:.0f}s", flush=True)
    return vecs, owners


def _score(qvecs, queries, qrels, vecs, owners):
    owners = np.array(owners)
    agg = {"ndcg": [], "recall": [], "mrr": []}
    for qi, qid in enumerate(queries):
        sims = vecs @ qvecs[qi]
        # best chunk score per doc
        best: dict = {}
        for s, d in zip(sims, owners):
            if s > best.get(d, -1e9):
                best[d] = s
        ranked = [d for d, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:K]]
        rel = qrels[qid]
        agg["ndcg"].append(_ndcg_at_k(ranked, rel, K))
        agg["recall"].append(_recall_at_k(ranked, rel, K))
        agg["mrr"].append(_mrr_at_k(ranked, rel, K))
    return {k: sum(v) / len(v) for k, v in agg.items()}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "scifact"
    from fastembed import TextEmbedding

    corpus, queries, qrels = _load(name)
    print(f"{name}: {len(corpus)} docs, {len(queries)} queries", flush=True)
    embed = TextEmbedding(model_name=BGE)
    qvecs = _norm(np.array(list(embed.embed([QPREFIX + queries[q] for q in queries])), dtype=np.float32))

    rows = {}
    for mode in ("whole", "chunk1200", "chunk2000"):
        vecs, owners = _build(corpus, embed, mode)
        rows[mode] = _score(qvecs, queries, qrels, vecs, owners)
        r = rows[mode]
        print(f"  [{mode}] nDCG@10={r['ndcg']:.4f} Recall@10={r['recall']:.4f} MRR@10={r['mrr']:.4f}", flush=True)

    print("\n" + "=" * 56)
    print(f"CHUNKING PROBE — BEIR/{name}, bge-768 dense-only, doc-level")
    print("=" * 56)
    print(f"{'config':<12}{'nDCG@10':>10}{'Recall@10':>11}{'MRR@10':>9}")
    for mode in ("whole", "chunk1200", "chunk2000"):
        r = rows[mode]
        print(f"{mode:<12}{r['ndcg']:>10.4f}{r['recall']:>11.4f}{r['mrr']:>9.4f}")
    print("=" * 56)


if __name__ == "__main__":
    main()
