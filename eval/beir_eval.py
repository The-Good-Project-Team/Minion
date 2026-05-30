"""Industry-standard retrieval eval: minion's actual retriever on a BEIR benchmark.

Scores the REAL minion retrieval models — fastembed `all-MiniLM-L6-v2` (dense) and
the `ms-marco-MiniLM-L-6-v2` cross-encoder rerank we added — on a standard BEIR
dataset (default: SciFact), with external relevance labels we cannot tune to. Reports
nDCG@10 / Recall@10 / MRR@10 as an ABLATION (dense-only vs dense+rerank) so we can see
whether the rerank actually creates value, against published baselines.

Run with minion's venv:
  cd chatgpt_mcp_memory && PYTHONPATH=src .venv/bin/python ../eval/beir_eval.py [dataset]

No external eval deps — fastembed (already a minion dep) + numpy. Dataset auto-downloads.
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent / "beir_data"
BEIR_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{name}.zip"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

# Published BEIR nDCG@10 baselines for context (from the BEIR paper / MTEB).
PUBLISHED = {
    "scifact": {"BM25": 0.665, "all-MiniLM-L6-v2 (dense)": 0.645},
    "nfcorpus": {"BM25": 0.325, "all-MiniLM-L6-v2 (dense)": 0.318},
}


def _download(name: str) -> Path:
    dest = DATA_DIR / name
    if (dest / "corpus.jsonl").exists():
        return dest
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    zpath = DATA_DIR / f"{name}.zip"
    print(f"downloading BEIR/{name} ...", flush=True)
    urllib.request.urlretrieve(BEIR_URL.format(name=name), zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(DATA_DIR)
    zpath.unlink(missing_ok=True)
    return dest


def _load(name: str):
    d = _download(name)
    corpus = {}
    with open(d / "corpus.jsonl") as fh:
        for line in fh:
            o = json.loads(line)
            corpus[o["_id"]] = (o.get("title", "") + " " + o.get("text", "")).strip()
    queries = {}
    with open(d / "queries.jsonl") as fh:
        for line in fh:
            o = json.loads(line)
            queries[o["_id"]] = o["text"]
    # qrels/test.tsv : query-id \t corpus-id \t score  (skip header)
    qrels: dict[str, dict[str, int]] = {}
    with open(d / "qrels" / "test.tsv") as fh:
        next(fh, None)
        for line in fh:
            qid, cid, score = line.strip().split("\t")
            if int(score) > 0:
                qrels.setdefault(qid, {})[cid] = int(score)
    # keep only queries that have judgments
    queries = {q: t for q, t in queries.items() if q in qrels}
    return corpus, queries, qrels


def _ndcg_at_k(ranked_ids, rel: dict[str, int], k: int) -> float:
    dcg = 0.0
    for i, cid in enumerate(ranked_ids[:k]):
        g = rel.get(cid, 0)
        if g:
            dcg += (2**g - 1) / math.log2(i + 2)
    ideal = sorted(rel.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def _recall_at_k(ranked_ids, rel: dict[str, int], k: int) -> float:
    if not rel:
        return 0.0
    hit = sum(1 for cid in ranked_ids[:k] if cid in rel)
    return hit / len(rel)


def _mrr_at_k(ranked_ids, rel: dict[str, int], k: int) -> float:
    for i, cid in enumerate(ranked_ids[:k]):
        if cid in rel:
            return 1.0 / (i + 1)
    return 0.0


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "scifact"
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    corpus, queries, qrels = _load(name)
    cids = list(corpus.keys())
    print(f"{name}: {len(corpus)} docs, {len(queries)} test queries", flush=True)

    print("embedding corpus (fastembed all-MiniLM-L6-v2) ...", flush=True)
    embed = TextEmbedding(model_name=EMBED_MODEL)
    doc_vecs = np.array(list(embed.embed([corpus[c] for c in cids])), dtype=np.float32)
    doc_vecs /= (np.linalg.norm(doc_vecs, axis=1, keepdims=True) + 1e-9)

    qids = list(queries.keys())
    q_vecs = np.array(list(embed.embed([queries[q] for q in qids])), dtype=np.float32)
    q_vecs /= (np.linalg.norm(q_vecs, axis=1, keepdims=True) + 1e-9)

    reranker = TextCrossEncoder(model_name=RERANK_MODEL)

    K, CAND = 10, 100
    dense = {"ndcg": [], "recall": [], "mrr": []}
    rerank = {"ndcg": [], "recall": [], "mrr": []}

    print(f"scoring {len(qids)} queries (dense top-{CAND} → rerank) ...", flush=True)
    for qi, qid in enumerate(qids):
        sims = doc_vecs @ q_vecs[qi]
        top = np.argsort(-sims)[:CAND]
        dense_ids = [cids[j] for j in top]
        rel = qrels[qid]
        dense["ndcg"].append(_ndcg_at_k(dense_ids, rel, K))
        dense["recall"].append(_recall_at_k(dense_ids, rel, K))
        dense["mrr"].append(_mrr_at_k(dense_ids, rel, K))

        scores = list(reranker.rerank(queries[qid], [corpus[c] for c in dense_ids]))
        order = np.argsort(-np.array(scores))
        rr_ids = [dense_ids[j] for j in order]
        rerank["ndcg"].append(_ndcg_at_k(rr_ids, rel, K))
        rerank["recall"].append(_recall_at_k(rr_ids, rel, K))
        rerank["mrr"].append(_mrr_at_k(rr_ids, rel, K))
        if (qi + 1) % 50 == 0:
            print(f"  {qi+1}/{len(qids)}", flush=True)

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print("\n" + "=" * 60)
    print(f"BEIR / {name}  —  minion retriever, n={len(qids)} queries")
    print("=" * 60)
    print(f"{'config':<26}{'nDCG@10':>10}{'Recall@10':>11}{'MRR@10':>9}")
    print(f"{'dense (all-MiniLM-L6-v2)':<26}{avg(dense['ndcg']):>10.4f}{avg(dense['recall']):>11.4f}{avg(dense['mrr']):>9.4f}")
    print(f"{'dense + cross-encoder':<26}{avg(rerank['ndcg']):>10.4f}{avg(rerank['recall']):>11.4f}{avg(rerank['mrr']):>9.4f}")
    lift = avg(rerank["ndcg"]) - avg(dense["ndcg"])
    print(f"\nrerank nDCG@10 lift: {lift:+.4f}  ({'ADDS VALUE' if lift > 0.005 else 'NO MEANINGFUL GAIN'})")
    if name in PUBLISHED:
        print("\npublished baselines (BEIR paper):")
        for k, v in PUBLISHED[name].items():
            print(f"  {k:<28}{v:>10.4f}")


if __name__ == "__main__":
    main()
