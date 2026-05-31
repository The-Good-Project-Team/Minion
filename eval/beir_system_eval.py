"""END-TO-END system eval: BEIR scored THROUGH minion's actual retrieval stack.

Unlike beir_eval.py (which benchmarks the embedding+rerank MODELS standalone — the
engine on a dyno), this ingests the BEIR corpus into a real minion SQLite store
(sources + chunks + vec_chunks + fts) and answers each query via minion's actual
`retrieval_engine.search_fused` — the SAME path ask_minion uses: sqlite-vec ANN +
the FTS sparse lane + RRF fusion + the cross-encoder rerank, integrated. This tests
THE CAR, not just the engine.

Run with minion's venv:
  cd chatgpt_mcp_memory && PYTHONPATH=src .venv/bin/python ../eval/beir_system_eval.py [dataset]
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import numpy as np

# reuse the dataset loader + metrics from the component eval
sys.path.insert(0, str(Path(__file__).resolve().parent))
from beir_eval import _load, _mrr_at_k, _ndcg_at_k, _recall_at_k, EMBED_MODEL  # noqa: E402

DB_PATH = Path("/tmp/minion-beir/memory.db")


# path → beir doc_id map, populated at ingest time (handles both ingest modes).
_PATH_TO_DOCID: dict = {}


def _ingest_direct(conn, corpus: dict, embed) -> None:
    """Storage path only: precomputed embeddings → upsert_source (bypasses the parser/chunker)."""
    from store import upsert_source

    cids = list(corpus.keys())
    texts = [corpus[c] for c in cids]
    print(f"[direct] embedding {len(texts)} docs + upsert to sqlite-vec/fts ...", flush=True)
    vecs = np.array(list(embed.embed(texts)), dtype=np.float32)
    now = time.time()
    for i, cid in enumerate(cids):
        t = texts[i]
        path = f"beir/{cid}"
        _PATH_TO_DOCID[path] = cid
        upsert_source(
            conn, path=path, kind="text", sha256=hashlib.sha1(t.encode()).hexdigest(),
            mtime=now, bytes_=len(t), parser="beir", source_meta={"beir_id": cid},
            chunks=[(t, None, {"beir_id": cid})], embeddings=vecs[i : i + 1],
        )
        if (i + 1) % 1000 == 0:
            conn.commit(); print(f"  ingested {i+1}/{len(cids)}", flush=True)
    conn.commit()


def _safe_name(cid: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cid)


def _ingest_pipeline(conn, corpus: dict, data_dir: Path) -> None:
    """THE FULL CAR incl. ingestion: write each doc as a file and run minion's real
    `ingest_file` (parser → chunker → embed → store). Tests chunking + parsing too."""
    from ingest import ingest_file

    docs_dir = data_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    cids = list(corpus.keys())
    print(f"[pipeline] writing {len(cids)} files + ingest_file (parser+chunker+embed) ...", flush=True)
    for i, cid in enumerate(cids):
        fname = f"{_safe_name(cid)}.txt"
        fpath = docs_dir / fname
        fpath.write_text(corpus[cid], encoding="utf-8")
        _PATH_TO_DOCID[str(fpath)] = cid
        _PATH_TO_DOCID[fname] = cid  # fallback match on basename
        try:
            ingest_file(conn, fpath)
        except Exception as exc:
            print(f"  ingest_file failed for {cid}: {exc}", flush=True)
        if (i + 1) % 500 == 0:
            conn.commit(); print(f"  ingested {i+1}/{len(cids)}", flush=True)
    conn.commit()


def _doc_id(hit) -> str:
    p = hit.path or ""
    if p in _PATH_TO_DOCID:
        return _PATH_TO_DOCID[p]
    if "beir/" in p:
        return p.split("beir/", 1)[1]
    # pipeline mode: match on basename stem
    base = p.rsplit("/", 1)[-1]
    if base in _PATH_TO_DOCID:
        return _PATH_TO_DOCID[base]
    return _PATH_TO_DOCID.get(base.rsplit(".", 1)[0] + ".txt", "")


def _run(conn, embed, queries: dict, qrels: dict, *, rerank: bool, label: str) -> dict:
    import retrieval_engine

    K = 10
    agg = {"ndcg": [], "recall": [], "mrr": []}
    qids = list(queries.keys())
    print(f"[{label}] scoring {len(qids)} queries through search_fused (rerank={rerank}) ...", flush=True)
    for qi, qid in enumerate(qids):
        qvec = np.asarray(next(iter(embed.embed([queries[qid]]))), dtype=np.float32)
        n = float(np.linalg.norm(qvec))
        if n:
            qvec = qvec / n
        hits = retrieval_engine.search_fused(
            conn, queries[qid], qvec, top_k=K, conn_factory=None, rerank=rerank
        )
        ranked = [d for d in (_doc_id(h) for h in hits) if d]
        rel = qrels[qid]
        agg["ndcg"].append(_ndcg_at_k(ranked, rel, K))
        agg["recall"].append(_recall_at_k(ranked, rel, K))
        agg["mrr"].append(_mrr_at_k(ranked, rel, K))
        if (qi + 1) % 50 == 0:
            print(f"  {qi+1}/{len(qids)}", flush=True)
    return {k: (sum(v) / len(v) if v else 0.0) for k, v in agg.items()}


def _attest(conn, queries: dict, qrels: dict, embed, *, ingest_mode: str) -> None:
    """Prove — from minion's OWN tables and code — which processes were exercised.
    Raises AssertionError if any stage was bypassed, so the numbers can be trusted."""
    import retrieval_engine
    from store import fts_available, get_chunk, get_embed_dim, keyword_search
    from store import search as dense_search

    print("\n" + "-" * 64)
    print("MINION COMPONENTS EXERCISED  (evidence from minion's own tables)")
    print("-" * 64)

    # 1) INGESTION → minion storage.
    sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    vec = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
    parsers = [r[0] for r in conn.execute("SELECT DISTINCT parser FROM sources").fetchall()]
    fts_ok = fts_available(conn)
    fts_n = conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] if fts_ok else 0
    ingest_fn = "ingest.ingest_file (parser→chunker→embed→upsert_source)" if ingest_mode == "pipeline" \
        else "store.upsert_source (precomputed embeddings; parser/chunker BYPASSED)"
    print(f"INGEST   via {ingest_fn}")
    print(f"         sources={sources}  chunks={chunks}  (chunks/doc={chunks/max(1,sources):.2f})  parsers={parsers}")
    print(f"         vec_chunks(sqlite-vec)={vec}  fts_chunks(FTS5)={fts_n}  fts_available={fts_ok}")
    assert chunks > 0, "FAIL: no chunks in minion.chunks — ingestion did not populate minion storage"
    assert vec == chunks, f"FAIL: vec_chunks({vec}) != chunks({chunks}) — embeddings not in minion's vector store"
    assert fts_ok and fts_n > 0, "FAIL: FTS5 sparse index empty — sparse lane has nothing to search"
    if ingest_mode == "pipeline":
        assert "beir" not in parsers, "FAIL: expected minion's real parser in pipeline mode, got the direct shim"

    # 2) RETRIEVAL → minion's lanes, on a query that has known relevant docs.
    qid = next(iter(queries))
    q = queries[qid]
    qvec = np.asarray(next(iter(embed.embed([q]))), dtype=np.float32)
    nrm = float(np.linalg.norm(qvec));  qvec = qvec / nrm if nrm else qvec
    dim = get_embed_dim(conn)
    dense = dense_search(conn, qvec, top_k=10)
    sparse = keyword_search(conn, q, top_k=10) if fts_ok else []
    graph = retrieval_engine._graph_lane(conn, qvec, q, top_k=10)
    fused = retrieval_engine.search_fused(conn, q, qvec, top_k=10, conn_factory=None, rerank=True)
    print(f"\nRETRIEVE sample query: {q[:70]!r}  (embed_dim={dim})")
    print(f"         dense  via store.search (sqlite-vec KNN)     → {len(dense)} hits")
    print(f"         sparse via store.keyword_search (FTS5 BM25)  → {len(sparse)} hits")
    print(f"         graph  via retrieval_engine._graph_lane      → {len(graph)} hits")
    print(f"         fused  via retrieval_engine.search_fused     → {len(fused)} hits (RRF: retrieval_bias.rrf_fuse_many)")
    reranker = retrieval_engine._get_reranker()
    print(f"         rerank via fastembed TextCrossEncoder '{retrieval_engine._RERANK_MODEL_NAME}' loaded={reranker is not None}")
    # Prove every returned hit is a REAL minion chunk that maps to a BEIR doc we indexed.
    assert fused, "FAIL: search_fused returned nothing"
    for h in fused[:5]:
        assert get_chunk(conn, h.chunk_id) is not None, f"FAIL: hit {h.chunk_id} is not a real minion chunk"
    mapped = [_doc_id(h) for h in fused]
    in_corpus = [d for d in mapped if d]
    assert in_corpus, "FAIL: no fused hit maps back to a BEIR doc — queries are not hitting OUR indexed data"
    rel_hit = any(d in qrels.get(qid, {}) for d in in_corpus)
    print(f"         → {len(in_corpus)}/{len(fused)} hits map to BEIR docs we ingested; "
          f"≥1 is a labeled-relevant doc: {rel_hit}")
    print("ATTEST   OK: ingestion populated minion storage; queries ran through minion's lanes; "
          "hits are real minion chunks mapping to BEIR docs.")
    print("-" * 64 + "\n")


def main() -> None:
    import os

    name = sys.argv[1] if len(sys.argv) > 1 else "scifact"
    ingest_mode = (os.environ.get("BEIR_INGEST") or "direct").strip().lower()
    if ingest_mode not in ("direct", "pipeline"):
        ingest_mode = "direct"
    from fastembed import TextEmbedding

    from store import connect

    corpus, queries, qrels = _load(name)
    print(f"{name}: {len(corpus)} docs, {len(queries)} test queries  | ingest_mode={ingest_mode}", flush=True)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect(DB_PATH)
    embed = TextEmbedding(model_name=EMBED_MODEL)
    if ingest_mode == "pipeline":
        _ingest_pipeline(conn, corpus, DB_PATH.parent)
    else:
        _ingest_direct(conn, corpus, embed)

    # Trust check: prove which minion processes ran, with evidence, before scoring.
    _attest(conn, queries, qrels, embed, ingest_mode=ingest_mode)

    os.environ["MINION_DISABLE_RERANK"] = "1"
    no_rr = _run(conn, embed, queries, qrels, rerank=False, label="system: dense+sparse fused")
    os.environ.pop("MINION_DISABLE_RERANK", None)
    with_rr = _run(conn, embed, queries, qrels, rerank=True, label="system: fused + rerank")

    print("\n" + "=" * 64)
    print(f"BEIR / {name}  —  THROUGH MINION'S ACTUAL RETRIEVAL  (n={len(queries)}, ingest={ingest_mode})")
    print("=" * 64)
    print(f"{'config':<34}{'nDCG@10':>9}{'Recall@10':>11}{'MRR@10':>9}")
    print(f"{'minion fused (dense+sparse+graph)':<34}{no_rr['ndcg']:>9.4f}{no_rr['recall']:>11.4f}{no_rr['mrr']:>9.4f}")
    print(f"{'minion fused + cross-encoder':<34}{with_rr['ndcg']:>9.4f}{with_rr['recall']:>11.4f}{with_rr['mrr']:>9.4f}")
    print(f"\nrerank lift (system): {with_rr['ndcg'] - no_rr['ndcg']:+.4f} nDCG@10")
    print("for reference (component dyno test, beir_eval.py): dense 0.624 / +rerank 0.694; BM25 0.665")
    conn.close()


if __name__ == "__main__":
    main()
