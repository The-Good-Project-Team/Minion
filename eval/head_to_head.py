"""Minion vs Minion 2 — head-to-head through the REAL retrieval stack.

Runs the same BEIR dataset through minion's actual ingest + search_fused path
once per embedder, so the only thing that changes is the embedding model:

    Minion 1  : sentence-transformers/all-MiniLM-L6-v2   (384-d)
    Minion 2  : BAAI/bge-base-en-v1.5                     (768-d)

Measures the three axes the product cares about:
  * faster at embedding  -> docs/sec + words/sec + wall time to embed the corpus
  * better at recall      -> nDCG@10 / Recall@10 / MRR@10 (fused, and fused+rerank)
  * cost                  -> embedding width (dim) + on-disk DB size (vec storage)

Embedding is local (fastembed/CPU) — there is no API token cost for embedding,
so "cost" here is compute (throughput) + storage (dim drives vec_chunks size).

Run with minion's venv:
  cd chatgpt_mcp_memory && PYTHONPATH=src .venv/bin/python ../eval/head_to_head.py [dataset]
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beir_eval import _load, _mrr_at_k, _ndcg_at_k, _recall_at_k  # noqa: E402

# (label, model_name, dim)
CONTENDERS = [
    ("Minion 1", "sentence-transformers/all-MiniLM-L6-v2", 384),
    ("Minion 2", "BAAI/bge-base-en-v1.5", 768),
]

K = 10


def _db_path(name: str, dim: int) -> Path:
    return Path(f"/tmp/minion-h2h-{name}-{dim}/memory.db")


def _ingest(conn, corpus: dict, embed, path_to_docid: dict) -> dict:
    """Embed every doc + upsert into sqlite-vec/fts. Returns timing/throughput."""
    from store import upsert_source

    cids = list(corpus.keys())
    texts = [corpus[c] for c in cids]
    total_words = sum(len(t.split()) for t in texts)

    t0 = time.perf_counter()
    vecs = np.array(list(embed.embed(texts)), dtype=np.float32)
    embed_secs = time.perf_counter() - t0

    now = time.time()
    for i, cid in enumerate(cids):
        t = texts[i]
        path = f"beir/{cid}"
        path_to_docid[path] = cid
        upsert_source(
            conn, path=path, kind="text", sha256=hashlib.sha1(t.encode()).hexdigest(),
            mtime=now, bytes_=len(t), parser="beir", source_meta={"beir_id": cid},
            chunks=[(t, None, {"beir_id": cid})], embeddings=vecs[i : i + 1],
        )
        if (i + 1) % 1000 == 0:
            conn.commit()
    conn.commit()
    return {
        "docs": len(cids),
        "words": total_words,
        "embed_secs": embed_secs,
        "docs_per_sec": len(cids) / embed_secs if embed_secs else 0.0,
        "words_per_sec": total_words / embed_secs if embed_secs else 0.0,
    }


def _doc_id(hit, path_to_docid: dict) -> str:
    src = hit.get("source") or hit.get("path") or ""
    base = src.split("#", 1)[0]
    if base in path_to_docid:
        return path_to_docid[base]
    return path_to_docid.get(base.rsplit(".", 1)[0] + ".txt", "")


def _score(conn, embed, model_name, queries, qrels, path_to_docid, *, rerank: bool) -> dict:
    import retrieval_engine
    from ingest import apply_query_prefix

    agg = {"ndcg": [], "recall": [], "mrr": []}
    for qid in queries:
        # bge wants a query-side instruction prefix; MiniLM gets the raw query.
        q_for_embed = apply_query_prefix(model_name, queries[qid])
        qvec = np.asarray(next(iter(embed.embed([q_for_embed]))), dtype=np.float32)
        n = float(np.linalg.norm(qvec))
        if n:
            qvec = qvec / n
        hits = retrieval_engine.search_fused(
            conn, queries[qid], qvec, top_k=K * 4, conn_factory=None, rerank=rerank
        )
        ranked: list = []
        seen: set = set()
        for h in hits:
            d = _doc_id(h, path_to_docid)
            if d and d not in seen:
                seen.add(d)
                ranked.append(d)
            if len(ranked) >= K:
                break
        rel = qrels[qid]
        agg["ndcg"].append(_ndcg_at_k(ranked, rel, K))
        agg["recall"].append(_recall_at_k(ranked, rel, K))
        agg["mrr"].append(_mrr_at_k(ranked, rel, K))
    return {k: (sum(v) / len(v) if v else 0.0) for k, v in agg.items()}


def _run_contender(label, model_name, dim, name, corpus, queries, qrels) -> dict:
    import os

    from fastembed import TextEmbedding
    from store import connect

    print(f"\n=== {label}: {model_name} ({dim}-d) ===", flush=True)
    db_path = _db_path(name, dim)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = connect(db_path, embed_dim=dim)
    embed = TextEmbedding(model_name=model_name)
    path_to_docid: dict = {}

    ing = _ingest(conn, corpus, embed, path_to_docid)
    print(f"  embedded {ing['docs']} docs in {ing['embed_secs']:.1f}s "
          f"({ing['docs_per_sec']:.1f} docs/s, {ing['words_per_sec']:.0f} words/s)", flush=True)

    os.environ["MINION_DISABLE_RERANK"] = "1"
    fused = _score(conn, embed, model_name, queries, qrels, path_to_docid, rerank=False)
    os.environ.pop("MINION_DISABLE_RERANK", None)
    reranked = _score(conn, embed, model_name, queries, qrels, path_to_docid, rerank=True)
    print(f"  fused nDCG@10={fused['ndcg']:.4f}  +rerank nDCG@10={reranked['ndcg']:.4f}", flush=True)

    conn.close()
    db_bytes = db_path.stat().st_size
    return {
        "label": label, "model": model_name, "dim": dim,
        "ingest": ing, "fused": fused, "reranked": reranked,
        "db_mb": db_bytes / (1024 * 1024),
    }


def _delta(a: float, b: float) -> str:
    """b relative to a, as a signed percent (positive = Minion 2 better/higher)."""
    if a == 0:
        return "  n/a"
    return f"{(b - a) / a * 100:+.0f}%"


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "scifact"
    corpus, queries, qrels = _load(name)
    print(f"{name}: {len(corpus)} docs, {len(queries)} test queries", flush=True)

    results = [_run_contender(lbl, m, d, name, corpus, queries, qrels) for lbl, m, d in CONTENDERS]
    m1, m2 = results[0], results[1]

    def row(metric: str, v1, v2, fmt: str, better_high=True):
        a, b = float(v1), float(v2)
        d = _delta(a, b) if better_high else _delta(b, a)
        print(f"{metric:<26}{format(a, fmt):>12}{format(b, fmt):>12}{d:>10}")

    print("\n" + "=" * 60)
    print(f"HEAD-TO-HEAD  —  BEIR/{name}  through minion's real retrieval")
    print("=" * 60)
    print(f"{'metric':<26}{m1['label']:>12}{m2['label']:>12}{'Δ (M2)':>10}")
    print("-" * 60)
    print(">> SPEED / COST")
    row("embed time (s)", m1['ingest']['embed_secs'], m2['ingest']['embed_secs'], ".1f", better_high=False)
    row("throughput (docs/s)", m1['ingest']['docs_per_sec'], m2['ingest']['docs_per_sec'], ".1f")
    row("throughput (words/s)", m1['ingest']['words_per_sec'], m2['ingest']['words_per_sec'], ".0f")
    row("embed width (dim)", m1['dim'], m2['dim'], "d", better_high=False)
    row("db size (MB)", m1['db_mb'], m2['db_mb'], ".1f", better_high=False)
    print(">> QUALITY (fused: dense+sparse+graph)")
    row("nDCG@10", m1['fused']['ndcg'], m2['fused']['ndcg'], ".4f")
    row("Recall@10", m1['fused']['recall'], m2['fused']['recall'], ".4f")
    row("MRR@10", m1['fused']['mrr'], m2['fused']['mrr'], ".4f")
    print(">> QUALITY (fused + cross-encoder rerank)")
    row("nDCG@10", m1['reranked']['ndcg'], m2['reranked']['ndcg'], ".4f")
    row("Recall@10", m1['reranked']['recall'], m2['reranked']['recall'], ".4f")
    row("MRR@10", m1['reranked']['mrr'], m2['reranked']['mrr'], ".4f")
    print("=" * 60)
    print("note: embedding is local (CPU/fastembed) — no API token cost; "
          "'cost' = compute throughput + vec storage (driven by dim).")


if __name__ == "__main__":
    main()
