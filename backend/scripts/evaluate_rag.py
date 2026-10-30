"""隔离的检索基线：复用真实切分、向量化与余弦排序，不读取或修改业务数据库。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.rag.embeddings import LocalHashEmbeddings, get_embeddings  # noqa: E402
from app.rag.retriever import DEFAULT_TOP_K, MIN_SCORE  # noqa: E402
from app.rag.splitter import split_markdown  # noqa: E402


def evaluate(provider, cases, *, top_k, min_score):
    """按分块证据标注评测召回与拒检；未知问题有返回即算误检。"""
    chunks = []
    hashes = {}
    for path in sorted((ROOT / "knowledge").glob("*.md")):
        content = path.read_text()
        hashes[path.name] = hashlib.sha256(content.encode()).hexdigest()
        chunks.extend({"source": path.name, "content": part, "chunk_index": index}
                      for index, part in enumerate(split_markdown(content)))
    for case in cases:
        for label in case["relevant"]:
            if not any(chunk["source"] == label["source"] and label["contains"] in chunk["content"]
                       for chunk in chunks):
                raise ValueError(f"{case['id']} 的证据标签已失效：{label}")
    vectors = np.asarray(provider.embed([chunk["content"] for chunk in chunks]), dtype=float)
    norms = np.linalg.norm(vectors, axis=1)
    rows = []
    for case in cases:
        started = time.perf_counter()
        vector = np.asarray(provider.embed([case["query"]])[0], dtype=float)
        scores = vectors @ vector / np.maximum(norms * np.linalg.norm(vector), 1e-12)
        indices = np.argsort(-scores, kind="stable")[:top_k]
        hits = [{**chunks[int(i)], "score": float(scores[i])} for i in indices if scores[i] >= min_score]
        matches = [any(hit["source"] == label["source"] and label["contains"] in hit["content"]
                       for label in case["relevant"]) for hit in hits]
        label_hits = sum(any(hit["source"] == label["source"] and label["contains"] in hit["content"]
                             for hit in hits) for label in case["relevant"])
        rows.append({**case, "hits": hits,
                     "recall": label_hits / len(case["relevant"]) if case["relevant"] else None,
                     "reciprocal_rank": next((1 / (i + 1) for i, matched in enumerate(matches) if matched), 0),
                     "rejected": not hits, "latency_ms": (time.perf_counter() - started) * 1000})
    positive = [row for row in rows if row["relevant"]]
    negative = [row for row in rows if not row["relevant"]]
    if not positive or not negative:
        raise ValueError("评测集必须同时包含有依据和无依据的问题")
    return {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "开发集上的离线检索基线；原始问题直接检索，未评测 Agent 查询改写、Qdrant、发布过滤和最终回答。",
            "provider": provider.name, "dimension": int(vectors.shape[1]),
            "top_k": top_k, "min_score": min_score, "corpus_sha256": hashes,
            "chunk_count": len(chunks), "positive_count": len(positive), "negative_count": len(negative),
            "recall_at_k": float(np.mean([r["recall"] for r in positive])),
            "mrr_at_k": float(np.mean([r["reciprocal_rank"] for r in positive])),
            "unknown_rejection_rate": float(np.mean([r["rejected"] for r in negative])),
            "known_false_rejection_rate": float(np.mean([r["rejected"] for r in positive])),
            "query_latency_ms_p50": float(np.percentile([r["latency_ms"] for r in rows], 50)),
            "query_latency_ms_p95": float(np.percentile([r["latency_ms"] for r in rows], 95)),
            "cases": rows}


def main():
    """默认强制使用本地向量；只有显式选择 configured 才可能调用收费 Embedding。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("local", "configured"), default="local")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/rag_cases.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/rag-baseline.json")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--min-score", type=float, default=MIN_SCORE)
    args = parser.parse_args()
    if args.top_k <= 0 or not -1 <= args.min_score <= 1:
        parser.error("top-k 必须大于 0，min-score 必须在 [-1, 1] 内")
    provider = LocalHashEmbeddings() if args.provider == "local" else get_embeddings()
    data = json.loads(args.cases.read_text())
    report = evaluate(provider, data["cases"], top_k=args.top_k, min_score=args.min_score)
    report["cases_sha256"] = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for name in ("provider", "chunk_count", "positive_count", "negative_count", "recall_at_k",
                 "mrr_at_k", "unknown_rejection_rate", "known_false_rejection_rate",
                 "query_latency_ms_p50", "query_latency_ms_p95"):
        print(f"{name}: {report[name]}")
    print(f"报告：{args.output}")


if __name__ == "__main__":
    main()
