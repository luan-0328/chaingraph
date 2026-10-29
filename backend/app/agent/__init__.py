"""agent：LangGraph Agent 层。

    state.py      AgentState（上下文载体）
    schemas.py    AgentDecision / AgentResult（结构化输出）
    llm.py        LLM 客户端（含结构化输出绑定）
    prompts.py    提示词
    nodes/        节点：analyze / retrieve / tools / result / human / action
    edges/        条件边：根据 State 决定下一步走哪个节点
    graph.py      把节点和边组装成图（带 Checkpoint）
    events.py     业务事件（落库 + SSE）
    progress.py   SSE 进度总线

铁律（v2.md §83）：
    Agent 不执行 SQL；要改业务必须走 Tool → Service → Repository。
    Graph 的流程由程序（条件边）控制，不是模型随便跳节点。
"""
