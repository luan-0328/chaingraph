"""按意图分组评测检索，仅使用开发集选择参数，不访问业务数据库。"""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import numpy as np
from app.rag.embeddings import LocalHashEmbeddings, get_embeddings
from app.rag.splitter import split_markdown

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def matches(chunk, label):
    return chunk["source"] == label["source"] and label["contains"] in chunk["content"]


def load_suite(path=ROOT / "evals/rag_suite.json", knowledge_dir=ROOT / "knowledge"):
    suite = json.loads(Path(path).read_text())
    chunks, corpus = [], {}
    for file in sorted(Path(knowledge_dir).glob("*.md")):
        content = file.read_text()
        corpus[file.name] = hashlib.sha256(content.encode()).hexdigest()
        chunks.extend(
            {"source": file.name, "chunk_index": i, "content": part}
            for i, part in enumerate(split_markdown(content))
        )
    cases = suite["cases"]
    ids, queries, groups = set(), set(), {}
    for case in cases:
        if case["id"] in ids or case["query"] in queries:
            raise ValueError(f"重复ID或问题：{case['id']}")
        ids.add(case["id"])
        queries.add(case["query"])
        split, group = case["split"], case["group_id"]
        if split not in ("dev", "test"):
            raise ValueError(f"无效划分：{split}")
        if group in groups and groups[group] != split:
            raise ValueError(f"意图组跨集：{group}")
        groups[group] = split
        evidence_ids = set()
        for evidence in case["evidence_groups"]:
            if evidence["id"] in evidence_ids or not evidence["any_of"]:
                raise ValueError(f"无效证据组：{case['id']}")
            evidence_ids.add(evidence["id"])
            for label in evidence["any_of"]:
                if not any(matches(chunk, label) for chunk in chunks):
                    raise ValueError(f"失效证据：{case['id']} / {label}")
    for split in ("dev", "test"):
        selected = [c for c in cases if c["split"] == split]
        if not any(c["evidence_groups"] for c in selected) or not any(
            not c["evidence_groups"] for c in selected
        ):
            raise ValueError(f"{split}必须同时有正例和未知问题")
    return cases, chunks, {"corpus_sha256": corpus, "suite_sha256": digest(suite)}


def prepare_vectors(cases, chunks, provider_name, fingerprint, cache_dir):
    from app.core.config import settings

    if provider_name == "local":
        provider = LocalHashEmbeddings()
        identity = {
            "name": provider.name,
            "dimension": provider.dim,
            "implementation": "ngram-v1",
        }
    else:
        if not settings.embedding_configured:
            raise ValueError("未配置语义Embedding，禁止将哈希回退误报为configured结果")
        provider = get_embeddings()
        identity = {
            "name": provider.name,
            "endpoint_sha256": digest(settings.embedding_base_url),
            "dimension_hint": settings.embedding_dim,
        }
    # 修改参考答案不需要重算向量；仅语料正文、问题顺序和模型变化使缓存失效。
    key = digest(
        {
            "corpus_sha256": fingerprint["corpus_sha256"],
            "queries": [c["query"] for c in cases],
            "provider": identity,
        }
    )
    path = Path(cache_dir) / f"{key}.npz"
    started = time.perf_counter()
    if path.exists():
        with np.load(path, allow_pickle=False) as saved:
            corpus, queries = saved["corpus"], saved["queries"]
        cached = True
    else:
        texts = [c["content"] for c in chunks] + [c["query"] for c in cases]
        vectors = []
        for offset in range(0, len(texts), 16):
            batch = provider.embed(texts[offset : offset + 16])
            if len(batch) != len(texts[offset : offset + 16]):
                raise ValueError("Embedding返回条数不符")
            vectors.extend(batch)
        matrix = np.asarray(vectors, dtype=float)
        if matrix.ndim != 2 or not matrix.shape[1] or not np.isfinite(matrix).all():
            raise ValueError("Embedding返回无效向量")
        corpus, queries = matrix[: len(chunks)], matrix[len(chunks) :]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp.npz")
        np.savez_compressed(temporary, corpus=corpus, queries=queries)
        temporary.replace(path)
        cached = False
    if corpus.shape[0] != len(chunks) or queries.shape[0] != len(cases):
        raise ValueError("缓存与语料不一致")
    corpus = corpus / np.maximum(np.linalg.norm(corpus, axis=1, keepdims=True), 1e-12)
    queries = queries / np.maximum(
        np.linalg.norm(queries, axis=1, keepdims=True), 1e-12
    )
    return queries @ corpus.T, {
        **identity,
        "configured_provider": provider_name,
        "dimension": int(corpus.shape[1]),
        "cache_hit": cached,
        "cache_key": key,
        "vector_preparation_seconds": round(time.perf_counter() - started, 4),
    }


def score_case(case, chunks, scores, top_k, min_score):
    indices = np.argsort(-scores, kind="stable")[:top_k]
    hits = [
        {**chunks[int(i)], "score": round(float(scores[i]), 4)}
        for i in indices
        if scores[i] > 0 and round(float(scores[i]), 4) >= min_score
    ]
    evidence = case["evidence_groups"]
    found = [any(matches(h, l) for h in hits for l in g["any_of"]) for g in evidence]
    relevant = [any(matches(h, l) for g in evidence for l in g["any_of"]) for h in hits]
    rank = next((i + 1 for i, value in enumerate(relevant) if value), None)
    return {
        **case,
        "hits": hits,
        "evidence_found": dict(zip((g["id"] for g in evidence), found)),
        "recall": sum(found) / len(found) if found else None,
        "all_evidence_found": bool(found) and all(found),
        "reciprocal_rank": 1 / rank if rank else 0,
        "rejected": not hits,
    }


def summarize(rows):
    known = [r for r in rows if r["evidence_groups"]]
    unknown = [r for r in rows if not r["evidence_groups"]]

    def mean(values):
        return float(np.mean(values)) if values else None

    return {
        "count": len(rows),
        "known_count": len(known),
        "unknown_count": len(unknown),
        "recall_at_k": mean([r["recall"] for r in known]),
        "all_evidence_rate": mean([r["all_evidence_found"] for r in known]),
        "mrr_at_k": mean([r["reciprocal_rank"] for r in known]),
        "unknown_rejection_rate": mean([r["rejected"] for r in unknown]),
        "known_false_rejection_rate": mean([r["rejected"] for r in known]),
    }


def evaluate_config(
    cases, chunks, scores, *, split, top_k, min_score, include_rows=False
):
    rows = [
        score_case(c, chunks, scores[i], top_k, min_score)
        for i, c in enumerate(cases)
        if c["split"] == split
    ]
    result = {
        "split": split,
        "top_k": top_k,
        "min_score": min_score,
        "metrics": summarize(rows),
    }
    buckets = defaultdict(list)
    for row in rows:
        buckets[row["kind"]].append(row)
    result["by_kind"] = {k: summarize(v) for k, v in buckets.items()}
    if include_rows:
        result["cases"] = rows
    return result


def selection_key(result):
    m = result["metrics"]
    balance = (m["all_evidence_rate"] + m["unknown_rejection_rate"]) / 2
    return (
        balance,
        -m["known_false_rejection_rate"],
        m["recall_at_k"],
        -result["top_k"],
        -result["min_score"],
    )


def run_experiment(cases, chunks, fingerprint, provider_names, cache_dir):
    runs, matrices, candidates = {}, {}, []
    for name in provider_names:
        scores, provider = prepare_vectors(cases, chunks, name, fingerprint, cache_dir)
        matrices[name] = scores
        grid = [
            evaluate_config(cases, chunks, scores, split="dev", top_k=k, min_score=t)
            for k in (2, 4, 6)
            for t in (
                0.05,
                0.1,
                0.15,
                0.2,
                0.25,
                0.3,
                0.35,
                0.4,
                0.45,
                0.5,
                0.55,
                0.6,
                0.65,
                0.7,
                0.75,
                0.8,
            )
        ]
        best = max(grid, key=selection_key)
        candidates.append((name, best))
        runs[name] = {"provider": provider, "dev_grid": grid, "selected_on_dev": best}
    name, best = max(candidates, key=lambda pair: selection_key(pair[1]))
    # 全部选择完成后才看test，不根据test回调参数。
    for provider_name in provider_names:
        scores = matrices[provider_name]
        runs[provider_name]["baseline_test"] = evaluate_config(
            cases, chunks, scores, split="test", top_k=4, min_score=0.05
        )
        runs[provider_name]["baseline_dev"] = evaluate_config(
            cases, chunks, scores, split="dev", top_k=4, min_score=0.05
        )
        chosen = runs[provider_name]["selected_on_dev"]
        runs[provider_name]["selected_test"] = evaluate_config(
            cases,
            chunks,
            scores,
            split="test",
            top_k=chosen["top_k"],
            min_score=chosen["min_score"],
            include_rows=True,
        )
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        **fingerprint,
        "scope": "离线精确余弦检索；未测Agent查询改写、Qdrant、发布过滤、工具和最终回答。test部分题源曾用于旧基线，不是严格盲测。",
        "selection_rule": "仅dev最大化(完整证据率+未知拒检率)/2，不自动应用生产。",
        "chunk_count": len(chunks),
        "case_count": len(cases),
        "runs": runs,
        "recommended_experiment": {
            "provider": name,
            "top_k": best["top_k"],
            "min_score": best["min_score"],
        },
    }


def render_markdown(report):
    lines = [
        "# RAG 自动评测结果",
        "",
        report["scope"],
        "",
        "| Provider | 阶段 | Top K | 阈值 | Recall@K | 完整证据率 | 未知拒检率 | 有据误拒率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, run in report["runs"].items():
        for key in (
            "baseline_dev",
            "selected_on_dev",
            "baseline_test",
            "selected_test",
        ):
            r = run[key]
            m = r["metrics"]
            values = [
                f"{m[k]:.1%}"
                for k in (
                    "recall_at_k",
                    "all_evidence_rate",
                    "unknown_rejection_rate",
                    "known_false_rejection_rate",
                )
            ]
            lines.append(
                f"| {name} | {key} | {r['top_k']} | {r['min_score']} | "
                + " | ".join(values)
                + " |"
            )
    lines += [
        "",
        "开发集选择的实验配置：`"
        + json.dumps(report["recommended_experiment"], ensure_ascii=False)
        + "`。未修改生产配置。",
        "",
        "## 留出集失败样本",
        "",
    ]
    for name, run in report["runs"].items():
        for row in run["selected_test"]["cases"]:
            if (row["evidence_groups"] and not row["all_evidence_found"]) or (
                not row["evidence_groups"] and not row["rejected"]
            ):
                lines.append(
                    f"- {name} / {row['id']}：{row['query']}（证据召回={row['recall']}，返回{len(row['hits'])}块）"
                )
    return "\n".join(lines) + "\n"
