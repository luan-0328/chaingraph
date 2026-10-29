"""services：业务逻辑层。

职责边界（v2.md §55）：
    Router → Service → Repository → DB
    Agent  → Tool    → Service    → Repository → DB

Service 层负责：
    - 业务规则（能不能退款、状态能否流转、库存够不够）
    - **事务边界**（session_scope 包住多个 Repository 操作）
    - 幂等保护（重复支付/重复退款/重复售后）
    - 归属校验（这条数据是不是当前用户的）

Service 不认识 HTTP，也不认识 LangGraph。
"""
