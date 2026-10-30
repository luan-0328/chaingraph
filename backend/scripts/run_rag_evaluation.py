"""标签校验、缓存、开发集调参与回归留出集报告。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.rag_evaluation import load_suite, render_markdown, run_experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers", nargs="+", choices=["local", "configured"], default=["local"]
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/rag-evaluation.json"
    )
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data/rag-eval")
    args = parser.parse_args()
    cases, chunks, fingerprint = load_suite()
    report = run_experiment(
        cases, chunks, fingerprint, list(dict.fromkeys(args.providers)), args.cache_dir
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    markdown = args.output.with_suffix(".md")
    markdown.write_text(render_markdown(report))
    print(render_markdown(report).split("## 留出集失败样本")[0])
    print(f"完整报告：{markdown}")


if __name__ == "__main__":
    main()
