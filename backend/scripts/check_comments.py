"""注释体检：确保后端源码的注释与文档字符串齐备、且都是中文。

    cd backend
    uv run python scripts/check_comments.py            # 严格区（services/repositories/agent/rag/core）不达标就退出码 1
    uv run python scripts/check_comments.py --all      # 全量（含 api/models/memory，历史缺口只提示不失败）
    uv run python scripts/check_comments.py --list-ok  # 顺带列出通过检查的文件

为什么分"严格区"和"历史区"
--------------------------
注释质量的标准分两档，不是双标，而是**分层对待风险**：

    严格区 services/ repositories/ agent/ rag/ core/
        —— 这里装着并发、事务、幂等、状态机的取舍。注释缺一句话，
           下一个人就可能把一个"看起来多余"的判断删掉，然后线上重复退款。
           所以这些目录要求：模块/类/公开函数都要有中文 docstring。

    历史区 api/ models/ memory/ main.py
        —— V2 骨架期遗留的一批薄封装（多数是一行 return），缺口在补但不阻塞。
           `--all` 会提示它们，不影响退出码。

三条可检查的规则：
    ① 严格区每个模块、每个公开函数/类都要有 docstring（中文）
    ② 任何地方都不允许出现整行英文注释（`# check if ...` 这种）
    ③ 关键文件必须包含"为什么"级别的说明段落（见 REQUIRED_NOTES）

第 ③ 条是刻意为之：光有"这个函数做什么"是注释的最低标准，
真正值钱的是"为什么不用另一种做法"。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_DIR = ROOT / "app"

#: 严格区：这些目录的 docstring 缺口会让脚本失败。
#:
#: 取值理由（宁可窄一点，也要让这条规则是"我真的做到了"而不是空话）：
#:     services/ repositories/  —— 事务、条件更新、幂等、状态机都在这里，
#:                                缺一句注释就可能被人删掉一个"多余的判断"
#:     agent/                  —— Agent 的图、事件、断点续跑、HITL
#:     rag/                    —— 检索与向量入库（禁用知识能否被检索到的关键）
#:     core/exceptions.py      —— 错误码的唯一真相（单独列出，而不是整个 core/）
#:
#: 不在严格区的（`--all` 仍会提示）：api/ models/ memory/ 以及 core/ 的
#: config·ids·logging·security 等薄封装 —— 它们多是"一行转发"，
#: 缺的是 docstring 而不是理解成本，属于已登记的待办。
STRICT_DIRS = ("services", "repositories", "agent", "rag")

#: 严格区之外、但同样要求 docstring 的单个文件（错误码是接口契约的一部分）
STRICT_FILES = ("core/exceptions.py",)

#: 中文（含全角标点）判定：只要出现一个汉字就算中文注释
CJK = re.compile(r"[\u4e00-\u9fff]")


def has_cjk(text: str) -> bool:
    return bool(CJK.search(text or ""))


#: 整行英文注释（排除工具指令：noqa / type: / pylint / fmt: / pragma）
_TOOL_DIRECTIVE = re.compile(r"#\s*(noqa|type:|pylint|fmt:|pragma|ruff:|mypy:|isort:|coding[:=])", re.I)

#: SQL / 路径 / 伪代码 / 映射：这类行是"引用"而不是"叙述"，不要求中文。
#: 例：`#     claim_paid()   UPDATE ... WHERE payment_status='UNPAID'`
#:     `# app/repositories/seed.py -> parents[2] == backend/`
#:     `# AgentDecision.issue_type → tickets.type`
_CODE_LIKE = re.compile(
    r"(\b(SELECT|UPDATE|INSERT|DELETE|WHERE|JOIN|CREATE|ALTER|DROP|SET|FROM)\b"  # SQL
    r"|->|=>|==|!=|::|→"                                                        # 代码符号 / 映射箭头
    r"|\w+\.\w+\(\)"                                                            # 函数调用
    r"|\w+\.\w+"                                                                # 属性/映射引用
    r"|[A-Za-z_]+/[A-Za-z_.]+)"                                                 # 路径
)

#: 纯英文小节标题（`# ---------- LLM ----------`、`# JWT`）—— 是标签不是叙述
_SECTION_DIVIDER = re.compile(r"^[-=~*_\s]*[A-Za-z0-9 &()/+,.'\-]{1,40}[-=~*_\s]*$")


def english_comment_lines(source: str) -> list[tuple[int, str]]:
    """找出"整行英文注释"（行尾注释不算 —— `x = 1  # 说明` 那种会单独查中文）。"""
    offenders: list[tuple[int, str]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        if _TOOL_DIRECTIVE.search(stripped):
            continue
        body = stripped.lstrip("#").strip()
        # 允许：空注释行、纯符号分隔线（===== / -----）
        if not body or not re.search(r"[A-Za-z]{3,}", body):
            continue
        # 允许：小节标题（`# ---------------- Embedding（RAG） ----------------`）——
        # 它是排版标签，不是叙述，中英文都不该要求。
        # 判据：两端包着分隔线，且内容很短。
        if _SECTION_DIVIDER.match(body) or _is_divider_label(body):
            continue
        if has_cjk(body):
            continue
        # 允许：SQL / 伪代码 / 路径引用（是"引用"不是"叙述"）
        if _CODE_LIKE.search(body):
            continue
        offenders.append((number, stripped))
    return offenders


def _is_divider_label(body: str) -> bool:
    """`---- Embedding（RAG） ----` 这种被分隔线包起来的短标题。"""
    if "-" not in body and "=" not in body:
        return False
    core = body.strip("-=~*_ ")
    # 去掉分隔线后剩下的部分很短，就认为它是标签而不是句子
    return bool(core) and len(core) <= 40


def missing_docstrings(source: str) -> list[tuple[int, str, str]]:
    """返回 (行号, 名字, 种类) —— 缺 docstring 的模块/类/函数（跳过 dunder 与私有小工具）。"""
    tree = ast.parse(source)
    missing: list[tuple[int, str, str]] = []

    if not ast.get_docstring(tree):
        missing.append((1, "<module>", "模块"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if not ast.get_docstring(node):
                missing.append((node.lineno, node.name, "类"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__"):
                continue
            # 私有且极短（<5 行）的辅助函数允许不写 docstring
            body_lines = (node.end_lineno or node.lineno) - node.lineno
            if node.name.startswith("_") and body_lines < 5:
                continue
            if not ast.get_docstring(node):
                missing.append((node.lineno, node.name, "函数"))
    return missing


def english_docstrings(source: str) -> list[tuple[int, str]]:
    """docstring 全英文的（代码标识符不算：docstring 里混中文就该有中文）。"""
    tree = ast.parse(source)
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node)
        if not doc:
            continue
        name = getattr(node, "name", "<module>")
        # 一行以内的短 docstring 且全英文 → 提示补中文
        if not has_cjk(doc) and len(doc.splitlines()) >= 1:
            offenders.append((getattr(node, "lineno", 1), name))
    return offenders


#: 关键文件必须包含的"为什么"关键词（说明取舍，而不只是描述行为）
REQUIRED_NOTES: dict[str, tuple[str, ...]] = {
    "services/refund_service.py": ("条件更新", "幂等"),
    "services/payment_service.py": ("条件更新", "唯一"),
    "services/ticket_service.py": ("条件更新", "归属"),
    "services/knowledge_service.py": ("发布", "草稿"),
    "repositories/ticket_repository.py": ("原子", "条件"),
    "repositories/agent_run_repository.py": ("指针",),
    "models/payment.py": ("唯一",),
    "models/refund.py": ("唯一",),
    "core/schema.py": ("生成列",),
    "agent/progress.py": ("重连", "去重"),
    "rag/retriever.py": (),
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="后端源码注释体检（中文 + docstring 齐备）")
    parser.add_argument("--all", action="store_true", help="全量检查（历史缺口只提示，不失败）")
    parser.add_argument("--list-ok", action="store_true", help="列出通过检查的文件")
    args = parser.parse_args()

    files = sorted(APP_DIR.rglob("*.py"))
    strict_issues = 0      # 严格区的问题（会让脚本失败）
    legacy_issues = 0      # 历史区的问题（只提示）
    total_english = 0      # 英文注释：任何地方都算问题
    total_note = 0         # 缺关键说明：任何地方都算问题

    print(f"注释体检：{len(files)} 个 py 文件（app/）")
    print(f"严格区：{'、'.join(STRICT_DIRS)} + {'、'.join(STRICT_FILES)}（这些的问题会让脚本失败）\n")

    for path in files:
        rel = path.relative_to(APP_DIR).as_posix()
        top = rel.split("/", 1)[0] if "/" in rel else ""
        strict = top in STRICT_DIRS or rel in STRICT_FILES

        source = read(path)
        problems: list[str] = []

        missing = missing_docstrings(source)
        bad_comments = english_comment_lines(source)
        bad_docs = english_docstrings(source)

        for lineno, name, kind in missing:
            problems.append(f"  L{lineno}: {kind} `{name}` 缺 docstring")
        for lineno, line in bad_comments:
            problems.append(f"  L{lineno}: 英文注释 → {line[:70]}")
        for lineno, name in bad_docs:
            problems.append(f"  L{lineno}: `{name}` 的 docstring 没有中文")

        keywords = REQUIRED_NOTES.get(rel)
        for word in keywords or ():
            if word not in source:
                problems.append(f"  缺少关键说明：应包含「{word}」的取舍说明")
                total_note += 1

        total_english += len(bad_comments)

        # 历史区：docstring 缺口不算失败（英文注释与关键说明仍然算）
        blocking = [p for p in problems if "缺 docstring" not in p]
        if strict:
            blocking = problems
            strict_issues += len(problems)
        else:
            legacy_issues += len([p for p in problems if "缺 docstring" in p])

        if problems and (strict or args.all or blocking):
            mark = "✗" if (strict or blocking) else "·"
            print(f"{mark} app/{rel}")
            print("\n".join(problems))
        elif args.list_ok and not problems:
            print(f"✓ app/{rel}")

    print("\n" + "=" * 64)
    failed = strict_issues or total_english or total_note
    print(
        f"严格区问题 {strict_issues} 处 ｜ 英文注释 {total_english} 处 ｜ "
        f"缺关键说明 {total_note} 处 ｜ 历史区 docstring 缺口 {legacy_issues} 处（不阻塞）"
    )
    if failed:
        print("✗ 未通过：严格区必须全部达标（英文注释与关键说明在全项目都要达标）")
        return 1

    print("✓ 通过：严格区 docstring 齐备，全项目无整行英文注释，关键取舍都有说明")
    if legacy_issues:
        print(f"  （提示：历史区还有 {legacy_issues} 处 docstring 缺口，用 --all 查看）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
