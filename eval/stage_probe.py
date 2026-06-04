"""Where does minion's OWN pipeline lose retrieval quality? Stage isolation.

Runs through the REAL surfaces (store.search, retrieval_engine.search_fused,
retrieval_bias.apply_identity_rerank, mcp_server._embed_query) against an
already-ingested DB, scoring each stage doc-level vs BEIR qrels. The stage
where nDCG/MRR craters is the culprit:

  dense        : store.search (sqlite-vec KNN) only
  +fusion      : search_fused(rerank=False)  -> adds sparse(BM25)+graph via RRF
  +rerank      : search_fused(rerank=True)    -> adds cross-encoder
  +identity    : apply_identity_rerank on top -> matches ask_minion exactly

  MINION_DATA_DIR=/tmp/m2-data MINION_EMBED_MODEL=BAAI/bge-base-en-v1.5 \
    PYTHONPATH=chatgpt_mcp_memory/src python eval/stage_probe.py scifact
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beir_eval import _load, _mrr_at_k, _ndcg_at_k, _recall_at_k  # noqa: E402

K = 10
POOL = 200  # candidate depth we ask each stage for


def _stem(p: str) -> str:
    b = str(p or "").split("/")[-1].split("#", 1)[0]
    return b.rsplit(".", 1)[0] if "." in b else b


def _docs_from_hits(hits) -> list:
    ranked, seen = [], set()
    for h in hits:
        d = _stem(getattr(h, "path", "") or "")
        if d and d not in seen:
            seen.add(d)
            ranked.append(d)
        if len(ranked) >= K:
            break
    return ranked


def _agg():
    return {"ndcg": [], "recall": [], "mrr": []}


def _add(agg, ranked, rel):
    agg["ndcg"].append(_ndcg_at_k(ranked, rel, K))
    agg["recall"].append(_recall_at_k(ranked, rel, K))
    agg["mrr"].append(_mrr_at_k(ranked, rel, K))


def _mean(agg):
    return {k: (sum(v) / len(v) if v else 0.0) for k, v in agg.items()}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "scifact"
    import mcp_server
    import retrieval_engine
    from retrieval_bias import apply_identity_rerank
    from store import search as dense_search

    conn = mcp_server._get_conn()
    _corpus, queries, qrels = _load(name)
    print(f"{name}: {len(queries)} queries through minion's own surfaces", flush=True)

    stages = {"dense": _agg(), "+fusion": _agg(), "+rerank": _agg(), "+identity": _agg()}
    t0 = time.perf_counter()
    for i, (qid, q) in enumerate(queries.items()):
        rel = qrels[qid]
        qvec = mcp_server._embed_query(q)

        _add(stages["dense"], _docs_from_hits(dense_search(conn, qvec, top_k=POOL)), rel)

        fnr = retrieval_engine.search_fused(conn, q, qvec, top_k=POOL,
                                            conn_factory=mcp_server._new_conn, rerank=False)
        _add(stages["+fusion"], _docs_from_hits(fnr), rel)

        frr = retrieval_engine.search_fused(conn, q, qvec, top_k=POOL,
                                            conn_factory=mcp_server._new_conn, rerank=True)
        _add(stages["+rerank"], _docs_from_hits(frr), rel)

        fid, _ = apply_identity_rerank(conn, list(frr))
        _add(stages["+identity"], _docs_from_hits(fid), rel)

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(queries)}  ({time.perf_counter()-t0:.0f}s)", flush=True)

    print("\n" + "=" * 58)
    print(f"STAGE PROBE — BEIR/{name} through minion's surfaces (bge, doc-level)")
    print("=" * 58)
    print(f"{'stage':<14}{'nDCG@10':>10}{'Recall@10':>11}{'MRR@10':>9}")
    for s in ("dense", "+fusion", "+rerank", "+identity"):
        m = _mean(stages[s])
        print(f"{s:<14}{m['ndcg']:>10.4f}{m['recall']:>11.4f}{m['mrr']:>9.4f}")
    print("=" * 58)
    print("(dense ~0.74 = model ceiling; whichever stage drops it is the bug)")


if __name__ == "__main__":
    main()
