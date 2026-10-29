"""repositories：数据访问层。

铁律（v2.md §55）：
    Router → Service → Repository → DB
    Agent  → Tool    → Service    → Repository → DB

禁止 Router 或 Agent 里出现 SQL。
每个函数第一个参数是 db: Session —— **事务边界属于 Service**，
Repository 只 flush，不 commit（commit 由 core.database.session_scope 负责）。
"""
