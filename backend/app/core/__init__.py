"""core：与业务无关的基础能力。

    config.py        配置（.env）
    database.py      引擎 / 事务 / 建库建表
    security.py      密码哈希 + JWT
    dependencies.py  当前用户 + 角色校验
    exceptions.py    业务异常（自带 code 与 HTTP 状态码）
    logging.py       日志（含 request_id）
    time.py          UTC 时间工具
"""
