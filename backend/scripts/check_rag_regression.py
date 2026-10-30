"""不调用外部模型的固定参数回归门禁；语料或标签变化必须显式复核基线。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.rag_evaluation import evaluate_config, load_suite, prepare_vectors


def check(reference, fingerprint, results):
    errors = []
    if any(
        reference.get(key) != fingerprint[key]
        for key in ("corpus_sha256", "suite_sha256")
    ):
        errors.append(
            "语料或标签指纹变化，请先审阅评测结果再显式更新基线；门禁不会自动放行"
        )
    for split, result in results.items():
        expected = reference["metrics"][split]
        for metric in (
            "recall_at_k",
            "all_evidence_rate",
            "mrr_at_k",
            "unknown_rejection_rate",
        ):
            if result["metrics"][metric] + 1e-8 < expected[metric]:
                errors.append(
                    f"{split}/{metric}退化：{result['metrics'][metric]:.4f} < {expected[metric]:.4f}"
                )
        if (
            result["metrics"]["known_false_rejection_rate"]
            > expected["known_false_rejection_rate"] + 1e-8
        ):
            errors.append(f"{split}/有据误拒率上升")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference", type=Path, default=ROOT / "evals/local-regression.json"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/rag-regression-result.json"
    )
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text())
    cases, chunks, fingerprint = load_suite()
    scores, provider = prepare_vectors(
        cases, chunks, "local", fingerprint, ROOT / "data/rag-eval"
    )
    results = {
        split: evaluate_config(
            cases,
            chunks,
            scores,
            split=split,
            top_k=reference["top_k"],
            min_score=reference["min_score"],
        )
        for split in ("dev", "test")
    }
    errors = check(reference, fingerprint, results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {"provider": provider, "errors": errors, "results": results},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(
        "\n".join(errors)
        if errors
        else "RAG固定参数回归通过（仅防退化，不代表生产质量达标）"
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
