"""V2.0 验收脚本：一条命令跑完 v2.md 的 Phase 19（异常与安全）+ Phase 20（场景 A~D）。

为什么是"HTTP 黑盒 + 真实模型"而不是纯单元测试？
    Phase 20 的四个场景本身就是端到端业务链路：
        注册→下单→支付→发货→收货、Agent 自动退款、RAG 问答、HITL 批准后恢复 Graph。
    只有打真实 HTTP、写真实 MySQL、调真实模型，才算"验收"——单测证明不了链路真的通。
    代价：脚本需要后端已启动、MySQL 可用、LLM_API_KEY 有效。

Phase 19 里的 "LLM 失败 / RAG 失败" 没法用正常实例测（正常实例不会失败），
所以脚本会**自己拉起两个"故障实例"**（默认开启，--no-faults 关闭）：
    :8001  LLM_API_KEY=sk-invalid                        → 验证 LLM 故障时的降级/落库
    :8002  EMBEDDING_* 指向不可达地址                     → 验证 RAG 故障时的降级
    两个实例共用同一个 MySQL，只是把"外部依赖"换成了坏的，属于真实的故障注入。
    端口已被占用时直接复用已有实例，不会重复启动。

用法：
    cd backend
    uv run python scripts/acceptance.py               # 全部（含 5 次真实 LLM 调用，约 3~5 分钟）
    uv run python scripts/acceptance.py --only A      # 只跑场景 A
    uv run python scripts/acceptance.py --only 18     # 只跑知识库（RAG 数据准备与检索）
    uv run python scripts/acceptance.py --only 19     # 只跑异常与安全
    uv run python scripts/acceptance.py --no-faults   # 跳过故障注入（LLM/RAG 失败项标记 SKIP）

注意：脚本会往库里写测试数据（随机账号 acc_*、订单、售后、工单），不会删除已有数据。
      结论看最后一行的「通过 N 项 / 失败 M 项」；日志走 stderr，
      在 PowerShell 里用 `2>&1` 重定向时 pwsh 可能把退出码记成 1（与测试结果无关）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_API = "http://127.0.0.1:8000/api"
HEALTH = "http://127.0.0.1:8000/health"

CS_LOGIN = {"username": "service01", "password": "123456"}
ADMIN_LOGIN = {"username": "admin", "password": "admin123"}

_passed: list[str] = []
_failed: list[str] = []
_skipped: list[str] = []


# ======================================================================
# 输出与断言
# ======================================================================


def section(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    (_passed if ok else _failed).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    _skipped.append(name)
    print(f"  ○ SKIP {name}  [{why}]")


def note(text: str) -> None:
    print(f"      · {text}")


# ======================================================================
# HTTP 客户端
# ======================================================================


class Api:
    """一个实例 = 一个登录身份。

    trust_env=False 很关键：本机开着系统代理时 httpx 会走代理，
    对 127.0.0.1 的请求会变成 502（实测踩过这个坑）。
    """

    def __init__(self, base_url: str = MAIN_API) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=180.0, trust_env=False)
        self.token: str | None = None

    def call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return self.client.request(method, path, headers=headers, **kw)

    def body(self, resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return resp.text

    def ok(self, method: str, path: str, **kw: Any) -> Any:
        """期望 2xx。失败直接抛：验收脚本不该带着脏状态继续往下跑。"""
        resp = self.call(method, path, **kw)
        payload = self.body(resp)
        if not 200 <= resp.status_code < 300:
            text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
            raise AssertionError(f"{method} {path} → HTTP {resp.status_code}: {text[:300]}")
        return payload

    def login(self, username: str, password: str) -> dict:
        payload = self.ok("POST", "/auth/login", json={"username": username, "password": password})
        self.token = payload["access_token"]
        return payload["user"]


def code_of(resp: httpx.Response) -> str:
    """取业务错误码（FastAPI 的 HTTPException 也会被统一成 {code, message}）。"""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return "-"
    return str(payload.get("code") or payload.get("detail") or "-")[:40]


def register(api: Api, prefix: str) -> str:
    """注册一个全新账号并登录（场景 A 的第一步就是"注册"，不依赖种子用户）。"""
    username = f"{prefix}_{uuid.uuid4().hex[:8]}"
    api.ok(
        "POST",
        "/auth/register",
        json={"username": username, "password": "123456", "nickname": username},
    )
    api.login(username, "123456")
    return username


def cheapest_product(api: Api, *, max_price: float = 500.0) -> dict:
    """挑一件"在售 + 有货 + 单价不超过自动退款上限"的商品。"""
    items = api.ok("GET", "/products", params={"limit": 50})["items"]
    candidates = [
        p for p in items if p["status"] == "ON_SALE" and p["stock"] > 0 and p["price"] <= max_price
    ]
    if not candidates:
        raise AssertionError(f"没有单价 ≤ {max_price} 元且有库存的在售商品，无法构造自动退款场景")
    return min(candidates, key=lambda p: p["price"])


def buy_and_pay(api: Api, *, max_price: float = 500.0) -> dict:
    """加购 → 下单 → 支付，返回订单详情（默认金额落在自动退款区间内）。"""
    product = cheapest_product(api, max_price=max_price)
    item = api.ok("POST", "/cart/items", json={"product_id": product["product_id"], "quantity": 1})
    order = api.ok("POST", "/orders", json={"item_ids": [item["item_id"]]})
    return api.ok("POST", f"/orders/{order['order_id']}/pay")


# ======================================================================
# 场景 A：完整购物
# ======================================================================


def scenario_a(api_base: str) -> None:
    section("场景A：完整购物（注册 → 登录 → 商品 → 购物车 → 下单 → 支付 → 发货 → 收货 → 完成）")
    user = Api(api_base)
    cs = Api(api_base)
    cs.login(**CS_LOGIN)

    username = register(user, "acc_a")
    check("注册 + 登录成功（全新账号）", bool(user.token), username)
    me = user.ok("GET", "/auth/me")
    check("账号角色为 USER", me["role"] == "USER", me["role"])

    products = user.ok("GET", "/products", params={"limit": 5})["items"]
    check("商品列表可访问", len(products) > 0, f"{len(products)} 件")

    order = buy_and_pay(user)
    check(
        "下单 + 支付成功",
        order["payment_status"] == "PAID" and order["fulfillment_status"] == "PENDING",
        f"order={order['order_id']} {order['payment_status']}/{order['fulfillment_status']} 金额={order['total_amount']}",
    )

    shipped = cs.ok("POST", f"/customer-service/orders/{order['order_id']}/ship")
    check("客服发货成功", shipped["fulfillment_status"] == "SHIPPED", shipped["fulfillment_status"])

    logistics = user.ok("GET", f"/orders/{order['order_id']}/logistics")
    check("物流信息已生成", bool(logistics), json.dumps(logistics, ensure_ascii=False)[:120])

    done = user.ok("POST", f"/orders/{order['order_id']}/confirm")
    check(
        "用户确认收货 → 订单完成",
        done["fulfillment_status"] == "COMPLETED",
        done["fulfillment_status"],
    )


# ======================================================================
# 场景 B：Agent 自动退款
# ======================================================================


def scenario_b(api_base: str) -> None:
    section("场景B：Agent 自动退款（订单 → 售后 → 工单 → Agent 查订单 + RAG → 自动退款 → 工单完成）")
    user = Api(api_base)
    register(user, "acc_b")

    order = buy_and_pay(user)
    sale = user.ok(
        "POST",
        "/after-sales",
        json={"order_id": order["order_id"], "type": "REFUND", "reason": "订单一直未发货，申请退款"},
    )
    check("创建售后单", sale["status"] == "APPLYING", f"{sale['after_sale_no']} {sale['status']}")

    ticket = user.ok(
        "POST",
        "/tickets",
        json={
            "content": f"我的订单 {order['order_no']} 一直没发货，我不要了，请退款",
            "order_id": order["order_id"],
            "after_sale_id": sale["after_sale_id"],
        },
    )
    check("创建工单（带上订单+售后上下文）", ticket["ticket_id"] > 0, f"ticket={ticket['ticket_id']}")

    resp = user.ok("POST", f"/tickets/{ticket['ticket_id']}/process")
    decision = resp.get("decision") or {}
    result = resp.get("result") or {}

    # 注意：AgentProcessResponse.status 是 **AgentRun** 的状态（COMPLETED/WAITING_HUMAN/FAILED），
    # 不是工单状态；工单状态下面单独查一次。
    check("AgentRun 处理完成（未转人工）", resp["status"] == "COMPLETED", resp["status"])
    check("决策识别为退款问题", decision.get("issue_type") == "REFUND", str(decision.get("issue_type")))
    note(f"业务动作：{result.get('business_action') or '(无)'}")
    note(f"处理方案：{(result.get('solution') or result.get('summary') or '')[:80]}")

    references = result.get("references") or []
    check("RAG 命中知识（references 非空）", len(references) > 0, f"{len(references)} 条")
    tools = result.get("tool_results") or []
    check("调用了业务工具（tool_results 非空）", len(tools) > 0, ", ".join(str(t.get("tool")) for t in tools))

    final_order = user.ok("GET", f"/orders/{order['order_id']}")
    check(
        "订单已自动退款（payment_status=REFUNDED）",
        final_order["payment_status"] == "REFUNDED",
        final_order["payment_status"],
    )

    sale_detail = user.ok("GET", f"/after-sales/{sale['after_sale_id']}")
    refund = (sale_detail.get("refunds") or [{}])[0]
    check(
        "退款流水 SUCCESS 且无人工审批（approved_by 为空）",
        refund.get("status") == "SUCCESS" and refund.get("approved_by") is None,
        f"status={refund.get('status')} approved_by={refund.get('approved_by')}",
    )

    final_ticket = user.ok("GET", f"/tickets/{ticket['ticket_id']}")
    check("工单结束（RESOLVED/CLOSED）", final_ticket["status"] in {"RESOLVED", "CLOSED"}, final_ticket["status"])


# ======================================================================
# 场景 C：Agent 规则问答
# ======================================================================


def scenario_c(api_base: str) -> None:
    section("场景C：Agent 规则问答（用户问题 → RAG → Agent 回答 → 用户确认 → 工单关闭）")
    user = Api(api_base)
    register(user, "acc_c")

    ticket = user.ok(
        "POST",
        "/tickets",
        json={"content": "请问七天无理由退货是怎么规定的？退货运费谁承担？"},
    )
    resp = user.ok("POST", f"/tickets/{ticket['ticket_id']}/process")
    decision = resp.get("decision") or {}
    result = resp.get("result") or {}

    check("决策走了 RAG（need_retrieval=true）", bool(decision.get("need_retrieval")), str(decision.get("need_retrieval")))
    check("未执行业务动作（纯问答，不下单退款）", not result.get("business_action"), str(result.get("business_action")))
    references = result.get("references") or []
    check("RAG 命中知识（references 非空）", len(references) > 0, f"{len(references)} 条")
    for ref in references[:3]:
        note(f"引用：{ref.get('title')}（{ref.get('source')}）score={ref.get('score')}")
    check("给出了面向用户的回答", bool(result.get("solution")), (result.get("solution") or "")[:60])

    user.ok("POST", f"/tickets/{ticket['ticket_id']}/messages", json={"content": "明白了，谢谢"})
    closed = user.ok("POST", f"/tickets/{ticket['ticket_id']}/close", json={"comment": "用户已确认解决"})
    check("用户确认后关闭工单", closed["status"] == "CLOSED", closed["status"])


# ======================================================================
# 场景 D：HITL 人工处理
# ======================================================================


def scenario_d(api_base: str) -> None:
    section("场景D：HITL 人工处理（复杂售后 → Agent 转人工 → 客服批准 → 恢复 Graph → 退款 → 工单关闭）")
    user = Api(api_base)
    register(user, "acc_d")
    cs = Api(api_base)
    cs_user = cs.login(**CS_LOGIN)

    order = buy_and_pay(user)
    # 关键：先发货。退款规则④"已发货需人工审核"，这样才必然走到 HITL 分支。
    cs.ok("POST", f"/customer-service/orders/{order['order_id']}/ship")

    sale = user.ok(
        "POST",
        "/after-sales",
        json={"order_id": order["order_id"], "type": "REFUND", "reason": "已经发货但商品有质量问题，要求退款"},
    )
    ticket = user.ok(
        "POST",
        "/tickets",
        json={
            "content": f"订单 {order['order_no']} 已经发货了，但收到的商品有质量问题，我要退款",
            "order_id": order["order_id"],
            "after_sale_id": sale["after_sale_id"],
        },
    )

    resp = user.ok("POST", f"/tickets/{ticket['ticket_id']}/process")
    check(
        "Agent 判定需要人工（HITL 挂起）",
        resp["status"] == "WAITING_HUMAN" and resp["need_human"] is True,
        f"status={resp['status']} need_human={resp['need_human']}",
    )
    check("AgentRun 记录了 thread_id（可恢复）", bool(resp.get("thread_id")), str(resp.get("thread_id")))
    if resp["status"] != "WAITING_HUMAN":
        note("未进入 WAITING_HUMAN，后续「批准 → 恢复 → 退款」的断言无意义，跳过")
        skip("客服批准后恢复 Graph 并退款", "Agent 未挂起")
        return

    approved = cs.ok(
        "POST",
        f"/tickets/{ticket['ticket_id']}/approve",
        json={"comment": "已核实商品质量问题，同意退款"},
    )
    check("客服批准成功", approved["status"] in {"RESOLVED", "PROCESSING", "CLOSED"}, approved["status"])

    final_order = user.ok("GET", f"/orders/{order['order_id']}")
    check("Graph 恢复后执行退款（订单已退款）", final_order["payment_status"] == "REFUNDED", final_order["payment_status"])

    sale_detail = user.ok("GET", f"/after-sales/{sale['after_sale_id']}")
    refund = (sale_detail.get("refunds") or [{}])[0]
    check(
        "退款流水 SUCCESS 且记录审批人（客服 id）",
        refund.get("status") == "SUCCESS" and refund.get("approved_by") == cs_user["user_id"],
        f"status={refund.get('status')} approved_by={refund.get('approved_by')} 客服id={cs_user['user_id']}",
    )

    final_ticket = user.ok("GET", f"/tickets/{ticket['ticket_id']}")
    check("工单推进到结束态", final_ticket["status"] in {"RESOLVED", "CLOSED"}, final_ticket["status"])


# ======================================================================
# Phase 19：异常与安全
# ======================================================================


def phase18_knowledge(api_base: str) -> None:
    """知识库：RAG 的数据准备与检索（管理端）。"""
    section("Phase 18：知识库（管理端 RAG 数据准备与检索）")
    admin = Api(api_base)
    admin.login(**ADMIN_LOGIN)

    docs = admin.ok("GET", "/admin/knowledge", params={"limit": 20})
    check("知识文档列表可读", docs["total"] >= 1, f"{docs['total']} 篇")
    check(
        "文档正文非空（能作为 RAG 语料）",
        all(doc["content"].strip() for doc in docs["items"]),
        f"{len(docs['items'])} 篇非空",
    )

    # 幂等：连续跑两次，第二次必须什么都不做（否则每次重启都会重复切分）
    admin.ok("POST", "/admin/knowledge/ingest")
    again = admin.ok("POST", "/admin/knowledge/ingest")
    check(
        "知识入库幂等（第二次不再重复切分）",
        again["documents"] == 0 and again["chunks"] == 0,
        f"documents={again['documents']} chunks={again['chunks']}",
    )

    result = admin.ok(
        "GET",
        "/admin/knowledge/search",
        params={"query": "七天无理由退货 退货运费谁承担", "top_k": 3},
    )
    hits = result["hits"]
    check("检索有命中（RAG 可用）", len(hits) > 0, f"{len(hits)} 条")
    if hits:
        top = hits[0]
        check(
            "命中带出处（标题 + 文件名 + 相似度）",
            bool(top.get("title")) and bool(top.get("source")) and top.get("score") is not None,
            f"{top.get('title')}（{top.get('source')}）score={round(float(top.get('score') or 0), 4)}",
        )


def phase19_security(api_base: str) -> None:
    section("Phase 19-1：权限 / 数据归属 / 重复提交 / 非法状态转换（主实例）")
    u1 = Api(api_base)
    register(u1, "acc_s1")
    u2 = Api(api_base)
    register(u2, "acc_s2")
    cs = Api(api_base)
    cs.login(**CS_LOGIN)

    # ---- 权限 ----
    anon = Api(api_base)
    resp = anon.call("GET", "/orders")
    check("未登录访问受保护接口 → 401", resp.status_code == 401, str(resp.status_code))
    resp = u1.call("GET", "/customer-service/dashboard")
    check("普通用户访问客服接口 → 403", resp.status_code == 403, str(resp.status_code))
    resp = u1.call("GET", "/admin/users")
    check("普通用户访问管理接口 → 403", resp.status_code == 403, str(resp.status_code))
    resp = cs.call("GET", "/admin/users")
    check("客服访问管理接口 → 403（角色分级）", resp.status_code == 403, str(resp.status_code))

    # ---- 数据归属 ----
    order = buy_and_pay(u1)
    resp = u2.call("GET", f"/orders/{order['order_id']}")
    check("他人订单不可读", resp.status_code in (403, 404), f"{resp.status_code} {code_of(resp)}")
    resp = u2.call("POST", f"/orders/{order['order_id']}/pay")
    check("他人订单不可支付", resp.status_code in (403, 404), f"{resp.status_code} {code_of(resp)}")
    resp = u2.call("POST", f"/orders/{order['order_id']}/cancel")
    check("他人订单不可取消", resp.status_code in (403, 404), f"{resp.status_code} {code_of(resp)}")
    resp = u2.call(
        "POST",
        "/after-sales",
        json={"order_id": order["order_id"], "type": "REFUND", "reason": "越权测试"},
    )
    check("他人订单不可申请售后", resp.status_code in (403, 404), f"{resp.status_code} {code_of(resp)}")

    # ---- 重复支付 ----
    resp = u1.call("POST", f"/orders/{order['order_id']}/pay")
    check("重复支付被拒（幂等，不会扣两次）", resp.status_code == 409, f"{resp.status_code} {code_of(resp)}")

    # ---- 非法状态转换 ----
    resp = u1.call("POST", f"/orders/{order['order_id']}/cancel")
    check("已支付订单不可取消（应走售后）", resp.status_code in (400, 409), f"{resp.status_code} {code_of(resp)}")
    resp = u1.call("POST", f"/orders/{order['order_id']}/confirm")
    check("未发货订单不可确认收货", resp.status_code in (400, 409), f"{resp.status_code} {code_of(resp)}")

    unpaid_product = cheapest_product(u2)
    unpaid_item = u2.ok("POST", "/cart/items", json={"product_id": unpaid_product["product_id"], "quantity": 1})
    unpaid = u2.ok("POST", "/orders", json={"item_ids": [unpaid_item["item_id"]]})
    resp = cs.call("POST", f"/customer-service/orders/{unpaid['order_id']}/ship")
    check("未支付订单不可发货", resp.status_code in (400, 409), f"{resp.status_code} {code_of(resp)}")

    # ---- 重复提交：同一商品重复加购应合并成一行 ----
    pid = unpaid_product["product_id"]
    u2.ok("POST", "/cart/items", json={"product_id": pid, "quantity": 1})
    u2.ok("POST", "/cart/items", json={"product_id": pid, "quantity": 1})
    cart = u2.ok("GET", "/cart")
    rows = [row for row in cart["items"] if row["product_id"] == pid]
    check(
        "重复加购合并为一行（不会生成重复下单行）",
        len(rows) == 1 and rows[0]["quantity"] == 2,
        f"行数={len(rows)} 数量={rows[0]['quantity'] if rows else 0}",
    )

    # ---- 重复退款 ----
    sale = u1.ok(
        "POST",
        "/after-sales",
        json={"order_id": order["order_id"], "type": "REFUND", "reason": "重复退款幂等测试"},
    )
    first = u1.ok("POST", f"/after-sales/{sale['after_sale_id']}/refund")
    check("首次退款自动执行（金额在自动区间且未发货）", first["executed"] is True and first["need_human"] is False, first["reason"])
    resp = u1.call("POST", f"/after-sales/{sale['after_sale_id']}/refund")
    check("重复退款被拒（不产生第二笔退款）", resp.status_code == 409, f"{resp.status_code} {code_of(resp)}")

    refunds = u1.ok("GET", "/after-sales", params={"limit": 20})["items"]
    mine = next(s for s in refunds if s["after_sale_id"] == sale["after_sale_id"])
    check("售后单终态为 COMPLETED", mine["status"] == "COMPLETED", mine["status"])


def phase19_concurrency(api_base: str) -> None:
    """并发重复提交：两个请求同时打同一个动作，只能有一个成功。"""
    section("Phase 19-2：并发重复提交（重复支付 / 重复退款）")
    user = Api(api_base)
    register(user, "acc_race")

    product = cheapest_product(user)
    item = user.ok("POST", "/cart/items", json={"product_id": product["product_id"], "quantity": 1})
    order = user.ok("POST", "/orders", json={"item_ids": [item["item_id"]]})

    pay = f"/orders/{order['order_id']}/pay"
    with httpx.Client(base_url=api_base, timeout=60.0, trust_env=False) as raw:
        headers = {"Authorization": f"Bearer {user.token}"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: raw.post(pay, headers=headers).status_code, range(2)))
    check(
        "并发支付：恰好一个成功",
        sorted(results) == [200, 409],
        f"状态码={results}",
    )

    sale = user.ok("POST", "/after-sales", json={"order_id": order["order_id"], "type": "REFUND", "reason": "并发退款测试"})
    refund_path = f"/after-sales/{sale['after_sale_id']}/refund"
    with httpx.Client(base_url=api_base, timeout=60.0, trust_env=False) as raw:
        headers = {"Authorization": f"Bearer {user.token}"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: raw.post(refund_path, headers=headers).status_code, range(2)))
    check(
        "并发退款：恰好一个成功",
        sorted(results) == [200, 409],
        f"状态码={results}",
    )


def phase19_tool_failure() -> None:
    """Tool 失败：工具层必须"返回结构化错误"，而不是把异常抛穿到图里。"""
    section("Phase 19-3：Tool 失败（进程内直调工具）")
    try:
        from app.tools.order_tools import query_order
        from app.tools.refund_tools import apply_refund
    except Exception as exc:  # noqa: BLE001
        skip("Tool 失败降级", f"导入工具失败：{exc}")
        return

    result = query_order.invoke({"order_id": 999_999_999, "user_id": 1})
    check(
        "查询不存在的订单 → 结构化错误（不抛异常）",
        isinstance(result, dict) and result.get("success") is False and bool(result.get("error")),
        json.dumps(result, ensure_ascii=False)[:140],
    )

    result = apply_refund.invoke({"order_id": 999_999_999, "user_id": 1, "amount": 1.0})
    check(
        "对不存在订单执行退款 → 结构化错误（不抛异常）",
        isinstance(result, dict) and result.get("success") is False and bool(result.get("error")),
        json.dumps(result, ensure_ascii=False)[:140],
    )


def phase19_llm_failure(fault_api: str) -> None:
    """LLM 失败：故障实例（LLM_API_KEY=sk-invalid）上跑一次 Agent。"""
    section("Phase 19-4：LLM 失败（故障实例 :8001）")
    user = Api(fault_api)
    register(user, "acc_llmfail")
    ticket = user.ok("POST", "/tickets", json={"content": "订单一直没发货，我要退款"})

    resp = user.call("POST", f"/tickets/{ticket['ticket_id']}/process")
    payload = user.body(resp)
    check(
        "LLM 故障：接口返回可读错误（code/message，不是裸崩溃）",
        isinstance(payload, dict) and bool(payload.get("code") or payload.get("message")),
        f"HTTP {resp.status_code} code={code_of(resp)}",
    )

    detail = user.ok("GET", f"/tickets/{ticket['ticket_id']}")
    check("LLM 故障：工单不会卡在 PROCESSING", detail["status"] != "PROCESSING", detail["status"])

    run = _latest_run(ticket["ticket_id"])
    if run is None:
        skip("AgentRun 落库为 FAILED", "读不到 agent_runs（数据库不可直连）")
        return
    check(
        "AgentRun 落库为 FAILED 且记录了错误原因",
        run["status"] == "FAILED" and bool(run["error"]),
        f"status={run['status']} error={(run['error'] or '')[:60]}",
    )


def phase19_rag_failure(fault_api: str) -> None:
    """RAG 失败：故障实例（Embedding 指向不可达地址）上跑一次问答。

    期望：知识检索失败被降级（无规则依据也能回答），工单照常完成。
    """
    section("Phase 19-5：RAG 失败（故障实例 :8002）")
    user = Api(fault_api)
    register(user, "acc_ragfail")
    ticket = user.ok("POST", "/tickets", json={"content": "七天无理由退货的运费谁承担？"})
    resp = user.call("POST", f"/tickets/{ticket['ticket_id']}/process")
    if not 200 <= resp.status_code < 300:
        check("RAG 故障：仍然给出回答（降级而不是整单失败）", False, f"HTTP {resp.status_code} code={code_of(resp)}")
        return
    payload = user.body(resp)
    result = (payload or {}).get("result") or {}
    check("RAG 故障：仍然给出回答（降级而不是整单失败）", bool(result.get("solution") or result.get("summary")), str((payload or {}).get("status")))
    check("RAG 故障：references 为空（没有伪造引用）", not (result.get("references") or []), f"{len(result.get('references') or [])} 条")

    run = _latest_run(ticket["ticket_id"])
    if run is None:
        skip("used_rag 标记为 0", "读不到 agent_runs（数据库不可直连）")
        return
    check("RAG 故障：used_rag 如实记为 0", int(run["used_rag"] or 0) == 0, str(run["used_rag"]))
    events = _run_events(run["id"])
    check(
        "RAG 故障：事件流里有降级说明（前端时间线可见）",
        any("知识检索失败" in (e or "") for e in events),
        " / ".join(events[-2:])[:120],
    )


def phase19_empty_structured_output() -> None:
    """实测故障：结构化输出偶发返回 None（供应商空响应）。

    旧代码会 `result.model_dump()` 直接炸 AttributeError → 接口 500，
    错误信息毫无可读性。这里把 LLM 换成"永远返回 None"来稳定复现，
    断言：会重试一次，并且抛出人能看懂的错误。
    """
    section("Phase 19-7：结构化输出为空（重试 + 可读错误）")
    try:
        from app.agent import llm as llm_module
        from app.agent.llm import ainvoke_structured
    except Exception as exc:  # noqa: BLE001
        skip("空结构化输出保护", f"导入失败：{exc}")
        return

    calls = {"n": 0}

    class _EmptyLlm:
        async def ainvoke(self, _messages):  # noqa: ANN001, ANN202
            calls["n"] += 1
            return None

    original = llm_module.resolve_structured_llm
    llm_module.resolve_structured_llm = lambda _schema: _EmptyLlm()  # type: ignore[assignment]
    raised: Exception | None = None
    try:
        asyncio.run(ainvoke_structured(dict, [{"role": "user", "content": "x"}], what="验收测试"))
    except Exception as exc:  # noqa: BLE001
        raised = exc
    finally:
        llm_module.resolve_structured_llm = original  # type: ignore[assignment]

    check(
        "空结构化输出 → 抛出可读错误（不再是 AttributeError）",
        isinstance(raised, RuntimeError) and "未返回结构化结果" in str(raised),
        f"{type(raised).__name__}: {raised}",
    )
    check("空结构化输出 → 自动重试一次", calls["n"] == 2, f"共调用 {calls['n']} 次")


def phase19_agent_failure(api_base: str) -> None:
    """非法恢复：对没有挂起中断的工单直接"批准"，必须给业务错误而不是 500。"""
    section("Phase 19-6：Agent 失败（对未挂起的工单执行 HITL 批准）")
    user = Api(api_base)
    register(user, "acc_hitlbad")
    cs = Api(api_base)
    cs.login(**CS_LOGIN)

    ticket = user.ok("POST", "/tickets", json={"content": "随便问一下发货时间"})
    resp = cs.call("POST", f"/tickets/{ticket['ticket_id']}/approve", json={"comment": "越权批准测试"})
    check(
        "对未挂起的工单批准 → 业务错误（4xx + 可读 code，不是 500）",
        resp.status_code < 500 and resp.status_code >= 400,
        f"{resp.status_code} {code_of(resp)}",
    )


# ======================================================================
# 直连数据库的补充断言（接口没有暴露这些字段）
# ======================================================================


def _connect():
    import pymysql
    from urllib.parse import urlparse

    from app.core.config import settings

    url = urlparse(settings.database_url.replace("mysql+pymysql://", "mysql://"))
    return pymysql.connect(
        host=url.hostname,
        port=url.port or 3306,
        user=url.username,
        password=url.password,
        database=(url.path or "/").lstrip("/"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _latest_run(ticket_id: int) -> dict | None:
    try:
        conn = _connect()
    except Exception:  # noqa: BLE001
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, error, used_rag, need_human FROM agent_runs "
                "WHERE ticket_id=%s ORDER BY id DESC LIMIT 1",
                (ticket_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _run_events(run_id: int) -> list[str]:
    try:
        conn = _connect()
    except Exception:  # noqa: BLE001
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT message FROM agent_run_events WHERE agent_run_id=%s ORDER BY id", (run_id,))
            return [row["message"] for row in cur.fetchall()]
    finally:
        conn.close()


# ======================================================================
# 故障实例（真实故障注入）
# ======================================================================


def _reachable(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3.0, trust_env=False).status_code < 500
    except Exception:  # noqa: BLE001
        return False


def ensure_fault_instance(port: int, overrides: dict[str, str], label: str) -> tuple[str | None, Any]:
    """拉起一个"外部依赖是坏的"后端实例；端口已被占用时直接复用。"""
    api_url = f"http://127.0.0.1:{port}/api"
    health = f"http://127.0.0.1:{port}/health"
    if _reachable(health):
        print(f"  （复用已在运行的故障实例 :{port} —— {label}）")
        return api_url, None

    env = {**os.environ, **overrides}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,  # 不用管道：避免和沙箱/管道限制纠缠
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):  # 最多等 40 秒
        if _reachable(health):
            print(f"  （已拉起故障实例 :{port} —— {label}）")
            return api_url, proc
        time.sleep(0.5)
    proc.terminate()
    return None, None


# ======================================================================
# main
# ======================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.md Phase 19/20 验收")
    parser.add_argument("--only", default="all", help="all | 18 | 19 | A | B | C | D")
    parser.add_argument("--api", default=MAIN_API, help="主实例 API 前缀")
    parser.add_argument("--no-faults", action="store_true", help="不启动故障实例（LLM/RAG 失败项标记 SKIP）")
    args = parser.parse_args()

    only = args.only.upper()
    want = lambda key: only in ("ALL", key)  # noqa: E731

    health = args.api.removesuffix("/api").rstrip("/") + "/health"
    if not _reachable(health):
        print(f"✗ 后端未启动：{health} 不可达。先运行：cd backend && uv run uvicorn app.main:app --reload")
        return 2

    started: list[Any] = []
    try:
        if want("A"):
            _run_phase("场景A", lambda: scenario_a(args.api))
        if want("B"):
            _run_phase("场景B", lambda: scenario_b(args.api))
        if want("C"):
            _run_phase("场景C", lambda: scenario_c(args.api))
        if want("D"):
            _run_phase("场景D", lambda: scenario_d(args.api))

        if want("18"):
            _run_phase("Phase18-知识库", lambda: phase18_knowledge(args.api))

        if want("19"):
            _run_phase("Phase19-权限/归属/幂等", lambda: phase19_security(args.api))
            _run_phase("Phase19-并发重复提交", lambda: phase19_concurrency(args.api))
            _run_phase("Phase19-Tool失败", phase19_tool_failure)
            _run_phase("Phase19-空结构化输出", phase19_empty_structured_output)
            _run_phase("Phase19-Agent失败", lambda: phase19_agent_failure(args.api))

            if args.no_faults:
                skip("LLM 失败", "--no-faults")
                skip("RAG 失败", "--no-faults")
            else:
                llm_api, llm_proc = ensure_fault_instance(
                    8001, {"LLM_API_KEY": "sk-invalid-acceptance-test"}, "LLM 故障"
                )
                if llm_proc:
                    started.append(llm_proc)
                if llm_api:
                    _run_phase("Phase19-LLM失败", lambda: phase19_llm_failure(llm_api))
                else:
                    skip("LLM 失败", "故障实例 :8001 起不来")

                rag_api, rag_proc = ensure_fault_instance(
                    8002,
                    {
                        "EMBEDDING_API_KEY": "sk-invalid-acceptance-test",
                        "EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
                        "EMBEDDING_MODEL": "text-embedding-3-small",
                    },
                    "Embedding 故障",
                )
                if rag_proc:
                    started.append(rag_proc)
                if rag_api:
                    _run_phase("Phase19-RAG失败", lambda: phase19_rag_failure(rag_api))
                else:
                    skip("RAG 失败", "故障实例 :8002 起不来")
    finally:
        for proc in started:
            proc.terminate()

    section("验收汇总")
    print(f"  通过 {len(_passed)} 项 / 失败 {len(_failed)} 项 / 跳过 {len(_skipped)} 项")
    for name in _failed:
        print(f"    ✗ {name}")
    for name in _skipped:
        print(f"    ○ {name}")
    print(
        "\n  说明：本脚本会写入测试数据（随机账号 acc_*、订单、售后、工单）。"
        "\n  RAG 当前使用本地降级向量器（未配 EMBEDDING_API_KEY），命中分数偏低属预期。"
    )
    return 1 if _failed else 0


def _run_phase(title: str, fn) -> None:  # noqa: ANN001
    """一个阶段炸了不要拖垮其它阶段：记一笔失败继续跑。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        check(f"{title} 整体执行失败", False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
