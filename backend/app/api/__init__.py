"""api：HTTP 接口层（Router）。

职责只有三件（v2.md §12）：
    1. 接收请求、用 schema 校验参数；
    2. 调用 Service；
    3. 返回响应（业务异常由 main.py 的全局处理器翻译成状态码）。

禁止：Router 里写 SQL、调用 LangGraph、实现业务规则。
"""
