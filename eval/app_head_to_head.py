"""Minion 1 vs Minion 2 — head-to-head against the REAL running app backends.

This is a pure HTTP client. It does NOT import minion's retrieval code — it
talks to two already-running Minion backends over their actual product API:

    1. feed a clean BEIR corpus into each app via  POST /ingest   (the app's own
       parser -> chunker -> embedder -> sqlite store does the work)
    2. wait for each app to finish indexing       (poll GET /status)
    3. ask each app every BEIR query via          POST /search
    4. score the app's returned ranking against the BEIR qrels, and time it.

So we measure the shipped products end-to-end (ingest speed, search latency,
recall/nDCG), not the embedding libraries in isolation.

Boot the two backends first (each with its own data dir + port), e.g.:

  # Minion 2 (current code)
  cd chatgpt_mcp_memory && MINION_DATA_DIR=/tmp/m2-data MINION_API_PORT=8912 \
      PYTHONPATH=src .venv/bin/python -m api

  # Minion 1 (old checkout, MiniLM-384)
  cd <minion1-worktree>/chatgpt_mcp_memory && MINION_DATA_DIR=/tmp/m1-data \
      MINION_API_PORT=8911 PYTHONPATH=src <venv>/bin/python -m api

Then:
  PYTHONPATH=chatgpt_mcp_memory/src python eval/app_head_to_head.py scifact \
      --m1-url http://127.0.0.1:8911 --m2-url http://127.0.0.1:8912
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
import json as _json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from beir_eval import _load, _mrr_at_k, _ndcg_at_k, _recall_at_k  # noqa: E402

K = 10


def _safe(cid: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cid)


def _post(url: str, payload: dict, timeout: float = 120.0) -> dict:
    data = _json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read().decode())


def _get(url: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return _json.loads(r.read().decode())


def _wait_up(base: str, tries: int = 60) -> None:
    for _ in range(tries):
        try:
            _get(f"{base}/status", timeout=5)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"backend never came up: {base}")


def _stage_corpus(corpus: dict) -> tuple[Path, dict]:
    """Write each BEIR doc to its own .txt so the app ingests a clean tree.
    Returns (dir, basename->docid map)."""
    out = Path(f"/tmp/minion-beir-clean/{int(time.time())}")
    out.mkdir(parents=True, exist_ok=True)
    name_to_doc: dict = {}
    for cid, text in corpus.items():
        fn = f"{_safe(cid)}.txt"
        (out / fn).write_text(text, encoding="utf-8")
        name_to_doc[fn] = cid
        name_to_doc[_safe(cid)] = cid  # match if extension is stripped
    return out, name_to_doc


def _ingest_and_wait(base: str, corpus_dir: Path, expected: int) -> float:
    """POST /ingest the whole tree, poll /status until indexing settles.
    Returns wall-clock seconds to fully index."""
    t0 = time.perf_counter()
    _post(f"{base}/ingest", {"path": str(corpus_dir), "recursive": True})
    last, stable = -1, 0
    while True:
        time.sleep(2)
        st = _get(f"{base}/status")
        srcs = int(st.get("counts", {}).get("sources", 0))
        active = st.get("active") or {}
        total = int(active.get("total", 0) or 0)
        done = int(active.get("done", 0) or 0)
        busy = total > 0 and done < total
        if srcs == last:
            stable += 1
        else:
            stable = 0
            last = srcs
        # settled: not actively ingesting AND source count stopped moving near target
        if not busy and stable >= 3 and srcs >= min(expected, 1):
            break
        if time.perf_counter() - t0 > 1800:
            print(f"  [{base}] ingest timeout at {srcs}/{expected} sources", flush=True)
            break
    return time.perf_counter() - t0


def _doc_of(row: dict, name_to_doc: dict) -> str:
    p = str(row.get("path") or "")
    base = p.split("/")[-1].split("#", 1)[0]
    if base in name_to_doc:
        return name_to_doc[base]
    return name_to_doc.get(base.rsplit(".", 1)[0], "")


def _score(base: str, queries: dict, qrels: dict, name_to_doc: dict) -> dict:
    agg = {"ndcg": [], "recall": [], "mrr": []}
    lat: list = []
    for qid, q in queries.items():
        t0 = time.perf_counter()
        try:
            resp = _post(f"{base}/search", {"query": q, "top_k": K * 4}, timeout=60)
        except Exception as exc:
            print(f"  [{base}] search failed for {qid}: {exc}", flush=True)
            continue
        lat.append((time.perf_counter() - t0) * 1000)
        ranked, seen = [], set()
        for row in resp.get("results", []):
            d = _doc_of(row, name_to_doc)
            if d and d not in seen:
                seen.add(d)
                ranked.append(d)
            if len(ranked) >= K:
                break
        rel = qrels[qid]
        agg["ndcg"].append(_ndcg_at_k(ranked, rel, K))
        agg["recall"].append(_recall_at_k(ranked, rel, K))
        agg["mrr"].append(_mrr_at_k(ranked, rel, K))
    out = {k: (sum(v) / len(v) if v else 0.0) for k, v in agg.items()}
    out["latency_ms_p50"] = sorted(lat)[len(lat) // 2] if lat else 0.0
    out["latency_ms_mean"] = sum(lat) / len(lat) if lat else 0.0
    return out


def _run(label: str, base: str, corpus_dir: Path, name_to_doc: dict,
         queries: dict, qrels: dict, expected: int) -> dict:
    print(f"\n=== {label}  ({base}) ===", flush=True)
    _wait_up(base)
    ver = _get(f"{base}/status").get("version", "?")
    print(f"  backend up (version {ver}); feeding {expected} BEIR docs ...", flush=True)
    ingest_s = _ingest_and_wait(base, corpus_dir, expected)
    st = _get(f"{base}/status")["counts"]
    print(f"  indexed in {ingest_s:.0f}s -> {st.get('sources')} sources / {st.get('chunks')} chunks", flush=True)
    print(f"  asking {len(queries)} queries via /search ...", flush=True)
    sc = _score(base, queries, qrels, name_to_doc)
    print(f"  nDCG@10={sc['ndcg']:.4f}  Recall@10={sc['recall']:.4f}  p50={sc['latency_ms_p50']:.0f}ms", flush=True)
    return {"label": label, "ingest_s": ingest_s, "sources": st.get("sources"),
            "chunks": st.get("chunks"), **sc}


def _delta(a: float, b: float, better_high=True) -> str:
    ref = a if better_high else b
    cmp = b if better_high else a
    if ref == 0:
        return "  n/a"
    return f"{(cmp - ref) / ref * 100:+.0f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", nargs="?", default="scifact")
    ap.add_argument("--m1-url", default="http://127.0.0.1:8911")
    ap.add_argument("--m2-url", default="http://127.0.0.1:8912")
    args = ap.parse_args()

    corpus, queries, qrels = _load(args.dataset)
    print(f"{args.dataset}: {len(corpus)} docs, {len(queries)} test queries", flush=True)
    corpus_dir, name_to_doc = _stage_corpus(corpus)
    print(f"staged clean corpus at {corpus_dir}", flush=True)

    m1 = _run("Minion 1", args.m1_url, corpus_dir, name_to_doc, queries, qrels, len(corpus))
    m2 = _run("Minion 2", args.m2_url, corpus_dir, name_to_doc, queries, qrels, len(corpus))

    def row(metric, k, fmt, better_high=True):
        a, b = float(m1[k]), float(m2[k])
        print(f"{metric:<24}{format(a, fmt):>12}{format(b, fmt):>12}{_delta(a, b, better_high):>10}")

    print("\n" + "=" * 58)
    print(f"APP HEAD-TO-HEAD  —  BEIR/{args.dataset}  (clean ingest via /ingest, asked via /search)")
    print("=" * 58)
    print(f"{'metric':<24}{'Minion 1':>12}{'Minion 2':>12}{'Δ (M2)':>10}")
    print("-" * 58)
    row("ingest time (s)", "ingest_s", ".0f", better_high=False)
    row("chunks indexed", "chunks", ".0f")
    row("search p50 (ms)", "latency_ms_p50", ".0f", better_high=False)
    row("search mean (ms)", "latency_ms_mean", ".0f", better_high=False)
    row("nDCG@10", "ndcg", ".4f")
    row("Recall@10", "recall", ".4f")
    row("MRR@10", "mrr", ".4f")
    print("=" * 58)


if __name__ == "__main__":
    main()
