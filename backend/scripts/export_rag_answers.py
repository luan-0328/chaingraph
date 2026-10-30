"""导出回答评测任务；有真实回答日志时转换为Ragas输入并执行可判定检查。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.rag_evaluation import load_suite


def export(report, provider, predictions=None):
    _, _, fingerprint = load_suite()
    if any(report[k] != fingerprint[k] for k in fingerprint):
        raise ValueError("评测报告与当前语料/标签不一致，必须重新评测")
    cases = report["runs"][provider]["selected_test"]["cases"]
    expected_ids = {c["id"] for c in cases}
    if predictions is not None and set(predictions) != expected_ids:
        raise ValueError("回答日志必须与留出集ID完全一致，禁止跳过失败题或重复计数")
    tasks, checks = [], []
    for case in cases:
        task = {
            "case_id": case["id"],
            "user_input": case["query"],
            "retrieved_contexts": [h["content"] for h in case["hits"]],
            "reference": case["reference_answer"],
        }
        if predictions is None:
            task.update(
                expected_behavior=case["expected_behavior"],
                required_tools=case["required_tools"],
                forbidden_phrase_review=case["must_not_claim"],
            )
        else:
            actual = predictions[case["id"]]
            if (
                not isinstance(actual.get("response"), str)
                or not actual["response"].strip()
            ):
                raise ValueError(f"{case['id']} 缺少真实非空response")
            # 必须记录实际使用的上下文，不能拿离线检索结果冒充线上模型上下文。
            if not isinstance(actual.get("retrieved_contexts"), list) or any(
                not isinstance(s, str) for s in actual["retrieved_contexts"]
            ):
                raise ValueError(f"{case['id']} 缺少实际retrieved_contexts")
            task["retrieved_contexts"] = actual["retrieved_contexts"]
            task["response"] = actual["response"]
            checks.append(
                {
                    "case_id": case["id"],
                    "behavior_matches": actual.get("behavior")
                    == case["expected_behavior"]
                    if "behavior" in actual
                    else None,
                    "required_tools_present": set(case["required_tools"])
                    <= set(actual["tool_calls"])
                    if "tool_calls" in actual
                    else None,
                    "forbidden_phrase_flags": [
                        phrase
                        for phrase in case["must_not_claim"]
                        if phrase in actual["response"]
                    ],
                }
            )
        tasks.append(task)
    return tasks, checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=ROOT / "docs/rag-evaluation.json"
    )
    parser.add_argument("--provider", default="configured")
    parser.add_argument("--answers", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/rag-answer-tasks.jsonl"
    )
    args = parser.parse_args()
    predictions = None
    if args.answers:
        rows = [
            json.loads(line)
            for line in args.answers.read_text().splitlines()
            if line.strip()
        ]
        predictions = {row["case_id"]: row for row in rows}
        if len(predictions) != len(rows):
            raise ValueError("回答日志包含重复case_id")
    tasks, checks = export(
        json.loads(args.report.read_text()), args.provider, predictions
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in tasks)
    )
    if checks:
        args.output.with_suffix(".checks.json").write_text(
            json.dumps(
                {
                    "scope": "行为/工具仅检查提供的日志；禁用短语只作人工复核告警，不等于语义错误判定。未执行LLM裁判。",
                    "cases": checks,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    print(
        f"导出{len(tasks)}条{'待运行任务' if predictions is None else '带真实回答的Ragas输入'}：{args.output}"
    )


if __name__ == "__main__":
    main()
