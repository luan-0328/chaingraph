"""断点续跑的安全判定（纯逻辑单测：不需要数据库、不需要后端，秒级跑完）。

    cd backend
    uv run python scripts/test_resume_guard.py

为什么单独一个脚本：
    "能不能从 Checkpoint 续跑"这个判断有 5 个条件，其中**最关键的那些分支正常流程
    永远跑不到**（图要么成功、要么停在人工确认点、要么副作用已经落地）。
    靠端到端脚本只能覆盖"能续跑"这一条，剩下 8 条只能这样直接喂假状态来验证。
    这个判定一旦放松（例如忘了拦 execute_action），后果是重复退款 —— 值得单独测。
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

# 让脚本从任意工作目录都能 import app.*（和同目录其它脚本保持一致）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.services.agent_service import _resume_decision  # noqa: E402

RUN = {"id": 1, "thread_id": "t-1"}


def snapshot(next_nodes, values, metadata=None):
    return SimpleNamespace(next=tuple(next_nodes), values=values or {}, metadata=metadata or {})


class FakeGraph:
    def __init__(self, snap):
        self._snap = snap

    async def aget_state(self, _config):
        return self._snap


def patch_graph(monkeypatch_snapshot):
    import app.services.agent_service as service

    original = service.get_graph
    service.get_graph = lambda: FakeGraph(monkeypatch_snapshot)
    return original


def run_case(title: str, snap, *, expect_resume: bool, monkeypatch_snapshot=None) -> None:
    import app.services.agent_service as service

    original = service.get_graph
    service.get_graph = lambda: FakeGraph(snap)
    try:
        result = asyncio.run(_resume_decision(RUN))
    finally:
        service.get_graph = original
    ok = (result is not None) == expect_resume
    print(f"  {'✓' if ok else '✗'} {title}  → {'续跑' if result else '从头跑'}")
    return ok


def main() -> int:
    settings.agent_resume_from_checkpoint = True
    results = []

    print("断点续跑的安全判定：")

    # ① 正常可续跑：只读节点跑过，图没跑完，没有副作用痕迹
    results.append(
        run_case(
            "只读节点跑过、图未结束 → 允许续跑",
            snapshot(["retrieve_knowledge"], {"decision": {}, "content": "x"}),
            expect_resume=True,
        )
    )

    # ② 已经跑到终态
    results.append(
        run_case("图已跑到终态（next 为空）→ 不续跑", snapshot([], {"decision": {}}), expect_resume=False)
    )

    # ③ Checkpoint 为空
    results.append(run_case("Checkpoint 是空的 → 不续跑", snapshot(["analyze"], {}), expect_resume=False))

    # ④ 停在人工确认点
    results.append(
        run_case(
            "停在人工确认点（need_human）→ 不续跑",
            snapshot(["human_review"], {"decision": {}, "need_human": True}),
            expect_resume=False,
        )
    )
    results.append(
        run_case(
            "State 里有 __interrupt__ → 不续跑",
            snapshot(["human_review"], {"__interrupt__": [{}]}),
            expect_resume=False,
        )
    )

    # ⑤ 副作用痕迹：这是最关键的一条
    results.append(
        run_case(
            "execute_action 已跑过（action_result 存在）→ 不续跑（避免重复退款）",
            snapshot(["generate_result"], {"result": {}, "action_result": {"executed": True}}),
            expect_resume=False,
        )
    )
    results.append(
        run_case(
            "人工审批已发生过（human_approved 存在）→ 不续跑",
            snapshot(["execute_action"], {"result": {}, "human_approved": True}),
            expect_resume=False,
        )
    )

    # ⑥ 开关关掉
    settings.agent_resume_from_checkpoint = False
    results.append(
        run_case(
            "开关 AGENT_RESUME_FROM_CHECKPOINT=false → 不续跑",
            snapshot(["retrieve_knowledge"], {"decision": {}}),
            expect_resume=False,
        )
    )
    settings.agent_resume_from_checkpoint = True

    # ⑦ 读 Checkpoint 抛异常 → 不续跑（但不能把重试搞失败）
    import app.services.agent_service as service

    class BoomGraph:
        async def aget_state(self, _config):
            raise RuntimeError("checkpoint 存储坏了")

    original = service.get_graph
    service.get_graph = lambda: BoomGraph()
    try:
        result = asyncio.run(_resume_decision(RUN))
    finally:
        service.get_graph = original
    ok = result is None
    results.append(ok)
    print(f"  {'✓' if ok else '✗'} 读 Checkpoint 报错 → 退回从头跑（不抛给调用方）")

    print(f"\n  通过 {sum(results)}/{len(results)} 项")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
