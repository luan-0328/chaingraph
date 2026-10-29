"""AgentState：在 LangGraph 节点之间流动的上下文载体（v2.md §28）。

四类 ID 的含义（v2.md §29）：
    user_id      谁
    ticket_id    哪个工单
    thread_id    这次 LangGraph 执行上下文（Checkpoint 的键）
    agent_run_id 这一次执行（审计/回溯）

State 里同时承载三类信息：
    输入      content / conversation / order_id / after_sale_id
    过程      decision / retrieved_context / references / tool_results
    输出      result / need_human / human_approved / status
"""

from typing import TypedDict


class AgentState(TypedDict, total=False):
    """节点之间流动的**全部**上下文（`total=False` = 字段都可缺省）。

    为什么用 total=False：State 是一路"长出来"的 ——
    analyze 之前没有 decision，retrieve 之前没有 references。
    强制每个节点都填全字段只会逼出一堆占位空值。

    两个字段的写法值得注意：

        conversation 使用覆盖语义
            —— 输入是完整的最近消息快照，重跑时不能把同一批消息重复累加。

        tool_results / references **没有** reducer
            —— 它们由单个节点一次性写完，覆盖语义正是我们要的
            （避免断点续跑时同一批结果被追加两遍）。

    与 Checkpoint 的关系：这个 dict 会被 LangGraph 序列化进 SQLite。
    所以**只放可序列化的数据**，不要塞 ORM 对象或连接。
    """

    # ---- 身份与上下文 ----
    user_id: int
    ticket_id: int
    order_id: int | None
    after_sale_id: int | None
    thread_id: str
    agent_run_id: int

    # ---- 输入 ----
    content: str                              # 工单内容
    conversation: list[dict]                  # 工单消息快照（最近若干条）

    # ---- 过程 ----
    decision: dict | None                     # AgentDecision
    analysis: str | None                      # 问题分析（来自 decision.summary 或结果）
    retrieved_context: list                   # RAG 命中的知识片段
    references: list                          # 引用元数据（前端展示"参考知识"）
    tool_results: list                        # 业务工具调用结果

    # ---- 输出 ----
    result: dict | None                       # AgentResult
    need_human: bool
    human_approved: bool | None
    human_comment: str | None
    human_operator_id: int | None
    human_review_round: int  # 同一 run 内递增；暂停重放不递增，实际完成本轮审批后递增。
    status: str

    # ---- 标记（写 AgentRun 用）----
    used_rag: bool
    used_tool: bool
    business_action: str | None
    action_result: dict | None
