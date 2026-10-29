# Agent 工单并发控制设计（同工单互斥 / 不同工单并发）

> 目标：**同一个 Ticket 的 Agent 执行互斥，不同 Ticket 的 Agent 执行并发。**
> 手段：**MySQL 原子状态抢占 + AgentRun 执行记录 + async Agent 执行。**
> 不引入 Redis / RabbitMQ / Celery / 线程池 / 全局锁。

本文是这套机制的权威说明；代码里的关键位置都有指向本文的注释。

---

## 1. 为什么不是"多线程 / 异步"解决

要解决的问题不是"怎么创建并发执行"，而是"**怎么保证同一张工单不被处理两次**"：

```text
请求 A ──→ Ticket 100
请求 B ──→ Ticket 100       两个请求，即使各跑一个线程/协程，也依然会重复执行
```

```text
线程 / async        → 解决"程序怎么执行"（I/O 等待期间谁能跑）
数据库状态控制      → 解决"业务并发一致性"（同一张工单能不能被处理两次）
```

Agent 的耗时几乎全在等 I/O（LLM API、向量检索、Tool 里的数据库/HTTP），
所以 Agent 本身继续用 `async def` + `await graph.ainvoke(...)`：不同工单在等 I/O 时自然交错执行。
但 **async 不负责互斥** —— 两个协程可以各自 await 在自己的等待里，
同时通过"先查一下状态再决定"的检查（这就是竞态）。
互斥只能由数据库的原子条件更新来提供。

---

## 2. 两个状态，两套枚举，职责不同

| | 含义 | 取值 | 谁在写 |
|---|---|---|---|
| `tickets.status` | 工单在**业务流程**里的位置 | OPEN / PROCESSING / WAITING_HUMAN / RESOLVED / CLOSED / FAILED | `TicketService` 的状态流转方法 |
| `tickets.agent_status` | 此刻**有没有 Agent 占着这张工单**（执行权） | IDLE / RUNNING / WAITING_HUMAN | 抢占 / 释放（见 §3、§4） |
| `agent_runs.status` | **某一次**执行走到哪一步了 | PENDING / **RUNNING** / WAITING_HUMAN / COMPLETED / FAILED / REJECTED | `AgentService` |

```text
status = PROCESSING 且 agent_status = IDLE      → 客服在手工处理，Agent 没在跑
status = PROCESSING 且 agent_status = RUNNING   → Agent 正在跑这一次执行
```

`tickets.agent_run_id` 记录"当前占据执行权的那一次 AgentRun"，
用于抢占失败时回答"现在跑的是哪一次"、以及收尾时校验"我还是不是持锁者"。

---

## 3. 核心 SQL：原子抢占

```sql
UPDATE tickets
   SET agent_status = 'RUNNING', agent_run_id = :run_id
 WHERE id = :ticket_id
   AND agent_status = 'IDLE';
```

然后看 **affected_rows**：

| affected_rows | 含义 | 动作 |
|---|---|---|
| 1 | 从 IDLE 抢成了 RUNNING | 创建 AgentRun，开始执行 |
| 0 | 已经是 RUNNING / WAITING_HUMAN，别人抢到了 | **不创建 AgentRun**，返回当前活跃的 Run |

**绝对不允许**这么写：

```python
if not is_running(ticket_id):        # 请求 A、B 都查到 IDLE
    create_run(ticket_id)            # 于是创建了两条活跃 AgentRun ← 这就是要修的 bug
```

MySQL 对同一行的 UPDATE 会串行化：只有第一个事务还能匹配到 `agent_status = 'IDLE'`，
其余事务的 affected_rows 是 0。实测（`backend/scripts/concurrency_check.py` 测试五）：
10 个请求用 Barrier 对齐后同时发 UPDATE，**成功数恰好 1**。

代码位置：

- `app/repositories/ticket_repository.py` → `acquire_agent_execution()` / `settle_agent_execution()`
- `app/services/ticket_service.py` → `acquire_agent_execution()` / `settle_agent_execution()`（唯一入口）

---

## 4. 释放：为什么必须 `finally`，以及怎么防"误放"

Agent 会遇到 LLM 超时、Tool 报错、RAG 挂了、进程异常……
只要有一条路径没释放，工单就永久卡在 `RUNNING`，用户再也点不动"开始处理"。

```python
if not ticket_service.acquire_agent_execution(ticket_id):
    return _reuse_active_run(ticket_id)     # 没抢到就返回已有 Run

try:
    ...
    return await _run_with_retries(...)
finally:
    _settle_after_execution(ticket_id, final_status=final_status)   # 成败都必须释放
```

收尾 SQL 带**归属校验**，只允许持锁的那次执行收尾：

```sql
UPDATE tickets
   SET agent_status = :final_status,
       agent_run_id = :keep_or_null          -- ← 见下：两种结局处理不同
 WHERE id = :ticket_id
   AND agent_status IN (:releasable)
   AND agent_run_id = :run_id;               -- ← 归属校验：不是我抢到的锁，我不动
```

`agent_run_id` 在两种结局下写法不同，这个区别是必须的：

| 收尾到 | `agent_run_id` | 为什么 |
|---|---|---|
| `IDLE` | `NULL` | 这次执行结束了，工单空闲，可以被重新处理 |
| `WAITING_HUMAN` | **保留 `run_id`** | 这次执行**没结束**，它正停在人工确认点 |

等待人工时保留指针不是"顺手留着"，而是三件事的前提：
① 客服**接管**要发一条带归属校验的条件更新（`WHERE agent_status='WAITING_HUMAN' AND agent_run_id=?`），
指针被清成 NULL 的话它永远匹配不到任何行 —— 接管会 100% 失败；
② 批准时要校验"工单此刻占着的还是原来那次执行"，避免批准打到别人的新执行上；
③ 排查问题时"这张工单现在等的是哪一次执行"应该能直接查出来。

结局 → 状态映射（`AgentService._settle_after_execution`）：

| 本次执行结局 | `agent_runs.status` | `tickets.agent_status` |
|---|---|---|
| 正常跑完 | COMPLETED | IDLE |
| 抛异常（含重试后仍失败） | FAILED | IDLE（用户可以重新发起 → 新建一次 AgentRun） |
| 停在人工确认点 | WAITING_HUMAN | WAITING_HUMAN（等客服审批，不允许另起一次） |
| 人工驳回 / 客服接管 | REJECTED | IDLE（交还客服人工处理） |

### "活跃执行"的判据：run 状态 **且** 工单指针

`find_active_run()` / `find_waiting_run()` 都同时看两件事：

```sql
SELECT * FROM agent_runs r JOIN tickets t ON t.id = r.ticket_id
 WHERE r.ticket_id = ? AND r.status IN ('PENDING','RUNNING','WAITING_HUMAN')
   AND t.agent_run_id = r.id;            -- ← 工单此刻必须真的指着它
```

只按 `r.status` 查会掉进一个**真实踩过的坑**：
某次执行停过 `WAITING_HUMAN`，后来被释放/接管（工单 `agent_status` 收回 IDLE、指针清空），
但 AgentRun 的历史是只增不改的 —— 库里就留下一条"状态 WAITING_HUMAN、工单早就不等它了"的记录。
只看状态会把它当成"还在等人工"，后果是：
客服重复接管被判成"状态已变化"而 100% 失败；用户重新点"处理"时被认为还在等人工，
永远起不来新执行 —— **一张工单从此彻底卡死**。

---

## 5. WAITING_HUMAN：不新建 Run，恢复**原** Run

```text
Agent 判定需要人工（例如"已发货订单要退款"）→ interrupt 挂起 + 落 Checkpoint
    tickets.agent_status = WAITING_HUMAN
    agent_runs.status    = WAITING_HUMAN
```

此时再点 `POST /tickets/{id}/process`：

```json
{ "status": "WAITING_HUMAN", "agent_run_id": 123, "used_existing_run": true }
```

**不会**创建第二个 AgentRun。审批走 `POST /tickets/{id}/approve`：

```text
WAITING_HUMAN ──(原子抢占，合法旧状态是 WAITING_HUMAN)──→ RUNNING
      ↓
Command(resume=...) 恢复**原 thread_id** 的图（Agent 之前的检索/工具/决策上下文都还在）
      ↓
COMPLETED ──→ agent_status = IDLE
```

要点：`process` 和 `approve` 抢的是**同一把锁**（`tickets.agent_status`），
区别只是允许的旧状态不同（`IDLE` vs `WAITING_HUMAN`）。
于是"客服连点两次批准"也只会真正恢复一次（第二次拿到 409 `AGENT_ALREADY_RUNNING`）。

### 5.1 人工接管的第三个出口：`takeover`

HITL 不是只有"批准/驳回"两个按钮（v2.md §40.1 明确列了三个）。
客服还要能说"这件事我来" —— `POST /tickets/{id}/takeover` 同时做两件事：
**终止 Agent 执行** + **把工单领到自己名下**（`assignee_id` = 操作客服，状态回 PROCESSING）。

```sql
-- 第一步：原子收回执行权（只允许从 WAITING_HUMAN 收，且必须还是那次 run）
UPDATE tickets SET agent_status='IDLE', agent_run_id=NULL
 WHERE id=:ticket_id AND agent_status='WAITING_HUMAN' AND agent_run_id=:run_id;
-- affected_rows = 1 → 我抢到了接管权，继续；
-- affected_rows = 0 → 409 TAKEOVER_CONFLICT（已被批准 / 已被别的客服接管 / 状态已变）
```

两个并发的接管请求里只有一个能改动那一行；连点两次时，第二次会发现
"工单已经归到该客服名下且处于 PROCESSING"，按**幂等**原样返回（不重复写消息、不重复改归属）。

一个容易忽略的细节：接管写进工单消息的文本要**始终包含"接管"这个动作**，
而不是只写客服的备注。只写备注的话，消息流里出现的是一句孤立的"我来处理"，
后来看工单的人无法判断这单什么时候、因为谁换了处理方式。

---

## 6. 僵尸执行：进程崩溃留下的 RUNNING 不会永久卡死

执行权是数据库里的一个字段，进程被 kill 时**没人执行 finally**。
所以 `agent_runs.alive_at` 是心跳（Agent 每产生一个业务事件就顺带刷新），
抢占失败时先判断"占着锁的那次执行是不是还活着"：

```text
心跳很新          → 真的在跑，老实返回当前 Run
心跳超过 30 分钟  → 判定僵尸：原子回收执行权（并把它标成 FAILED），然后重抢一次
```

回收本身也是带条件的原子 UPDATE（`WHERE agent_status='RUNNING' AND agent_run_id=?`），
多个请求同时来回收时只有一个能改动成功，不会误伤别人的新执行。

一个实测踩到的坑：**MySQL 的 `NOW()` 是服务器本地时间，而应用统一写 UTC**。
如果有人（或某次排查）用 `NOW()` 写了心跳，算出来的"心跳年龄"会是负数 ——
负数意味着"最新鲜的心跳"，绝不能当成僵尸去抢别人的工单。
`ticket_service._is_run_alive()` 对"未来的心跳"打警告并按"仍在执行"处理。

---

## 7. 失败与重试：区分可恢复 / 不可恢复

```text
可恢复：LLM 超时 / 429 / 连接被重置 / 503  → 自动重试（AGENT_MAX_RETRIES，默认 2）
不可恢复：订单不存在 / 退款条件不满足 / 权限不足 / 鉴权失败 → 立刻失败，把原因讲清楚
```

判定逻辑在 `AgentService.is_retryable_error()`（保守：拿不准就不重试）。

### 7.1 重试优先"从断点续跑"

```text
第 1 轮：analyze ✅ → retrieve ❌（LLM/依赖抖动）→ 图中断，Checkpoint 停在这儿
第 2 轮：ainvoke(None, thread_id) → 从 retrieve 继续
         analyze 不重跑，已经查到的 RAG / 工具结果继续用
```

`ainvoke(None, ...)` 与 `ainvoke(state, ...)` 的差别就是"接着跑"和"从头跑"。
续跑安全性的判定在 `_resume_decision()`，五条全部满足才续跑：

| # | 条件 | 不满足时 |
|---|---|---|
| ① | `AGENT_RESUME_FROM_CHECKPOINT=true`（默认开） | 从头跑 |
| ② | 该 thread_id 在 Checkpoint 里有状态 | 从头跑 |
| ③ | 图没跑到终态（`next` 非空） | 无需续跑 |
| ④ | 没停在人工确认点（`need_human` / `__interrupt__`） | 那是正常暂停，不做失败重试 |
| ⑤ | State 里没有副作用痕迹（`action_result` / `human_*`） | **从头跑** |

第 ⑤ 条是硬条件：`execute_action` 会真的调退款与写工单消息，如果它在"退款成功、
写消息失败"的缝隙里炸掉，续跑就会再退一次（退款规则会拦下来，但那是业务幂等兜底，
不该当成重试策略的前提）。

### 7.2 重试不新建 AgentRun

`agent_runs.thread_id` 上有唯一约束 —— 一次执行的 LangGraph 上下文只对应一条执行记录，
断点续跑复用同一个上下文，所以它**必然是同一条 AgentRun 的继续**：

```text
用户重新点"开始处理"   → 新 thread_id、新 AgentRun（外部新执行）
系统内部重试           → 同一个 thread_id、同一条 AgentRun（retry_count +1、resumed = true）
```

失败详情留在 `agent_run_events` 里（不会因为最终成功就丢掉），
`agent_runs.error` 在续跑成功后清空 —— 它表达的是"这条执行最终的失败原因"。

---

## 8. 一次请求的完整流程

```text
POST /tickets/{id}/process
        │
        ├─ 查工单（含归属校验）
        │
        ├─ 原子抢占 agent_status: IDLE → RUNNING
        │     ├─ 失败 → 是僵尸吗？是 → 回收后重抢一次
        │     │                   否 → 返回当前活跃 Run（status=ALREADY_RUNNING / WAITING_HUMAN）
        │     └─ 成功 ↓
        │
        ├─ 创建 AgentRun（PENDING → PROCESSING，打心跳）
        ├─ 落 tickets.agent_run_id（绑定这次执行）
        │
        ├─ AgentService 执行：组装 State → LangGraph(ainvoke) → LLM / RAG / Tool
        │     └─ 可恢复异常 → 新建 AgentRun 重试（最多 AGENT_MAX_RETRIES 次）
        │
        └─ finally：按结局把执行权落成 IDLE 或 WAITING_HUMAN（带归属校验）
```

`AgentService` 不直接改工单状态：它只汇报"本次执行是什么结局"，
由 `TicketService` 决定落成什么状态（`app/services/ticket_service.py` 的注释里写明了这条分工）。

---

## 9. 为什么没有全局锁

```python
global_lock = asyncio.Lock()      # ❌ 不要这么写
async with global_lock:
    await process_ticket(ticket_id)
```

那样会把"不同工单可以并发"一起锁死：Ticket 100 跑 30 秒，Ticket 101 只能排队。

本方案的"锁"就是**工单自己那一行**：锁的粒度天然等于业务实体，
不同工单互不影响，也不需要任何额外的中间件。
真正的多实例部署时这套机制**依然成立**（抢占在数据库里，不依赖进程内状态）；
只有当瓶颈变成"数据库行锁竞争"或需要跨实例广播进度时，
才需要考虑 Redis 分布式锁 / 消息队列 —— 而不是为了简历提前引入。

---

## 10. 怎么验收

```powershell
cd backend

# pytest 回归网（85 项，自己拉起被测实例，不需要先启服务）
uv run pytest
uv run pytest tests/test_concurrency_money.py       # 只看"钱和货"的并发与幂等
uv run pytest -m agent                              # 只看 Agent 重试 / 断点续跑 / HITL

# 黑盒脚本验收
uv run python scripts/concurrency_check.py          # 66 项断言，约 2 分钟，不需要 LLM_API_KEY
uv run python scripts/concurrency_check.py --only 5 # 只跑最核心的"并发竞争"
uv run python scripts/concurrency_check.py --keep   # 保留测试实例（:8010）调试

uv run python scripts/test_resume_guard.py          # 断点续跑的安全判定（9 项，纯逻辑，秒级）
uv run python scripts/test_side_effect_guard.py     # 副作用幂等键行为（7 项，含并发抢键）
uv run python scripts/concurrency_safety.py         # 资金与库存的并发安全（25 项，需 :8000 已启动）
uv run python scripts/e2e_chain.py                  # 端到端链路 24 项（确定性桩，不花钱）
uv run python scripts/e2e_chain.py --with-llm       # 同上，但用真实模型
```

`concurrency_check.py` 会自己拉起一个**带确定性 LLM 桩**的实例（`:8010`，`AGENT_STUB_LLM=1`）：
桩把每次"模型调用"固定成 `sleep 1.2s`，于是"并发"这件事可测量 ——
真实 LLM 的耗时抖动会让并发窗口时有时无，那样的测试证明不了任何事。
被测的其它部分（MySQL 抢占、finally 释放、心跳、HITL 恢复原图、断点续跑、落库、SSE）全是真实代码。

| # | 测试 | 断言 |
|---|---|---|
| 一 | 同一工单 10 并发 | 只创建 1 个 AgentRun；1 个执行 + 9 个 `ALREADY_RUNNING`；结束后 `agent_status=IDLE` |
| 二 | 10 张不同工单并发 | 10 个不同 AgentRun；总耗时 ≈ 单次耗时（实测 3.7s，串行需 24s）→ 证明没有被全局锁串起来 |
| 三 | Agent 抛异常 | AgentRun=FAILED 且带错误原因；`agent_status=IDLE`；工单不卡 PROCESSING；可重新发起 |
| 四 | WAITING_HUMAN | 再点处理不新建 Run 并返回原 run_id；批准后**仍是同一条 AgentRun** 且推进到 COMPLETED |
| 五 | 并发竞争（10 请求同工单） | 数据库里只有一个活跃 Run，恰好 1 个请求真的执行 |
| 六 | 断点恢复 | 注入"检索节点首次调用失败"：整体仍成功、`retry_count>=1`、`resumed=true`、**「正在分析工单」只出现一次**且排在「从断点续跑」之前（换成 generate_result 节点再验一遍） |
| 附加 | 重试策略 | 可恢复异常重试后成功（同一条 run）；不可恢复异常只尝试一次；失败详情留在事件流 |
| 附加 | 僵尸执行回收 | 手工制造"心跳停在 2 小时前的 RUNNING"，请求能自愈并把僵尸 Run 标成 FAILED |

`concurrency_safety.py`（业务并发安全，HTTP 黑盒 + 直连数据库断言）：

| # | 测试 | 断言 |
|---|---|---|
| 1 | 重复支付 | 4 个并发支付只成功 1 个；`payments` 只有 1 条 SUCCESS 流水 |
| 2 | 重复退款 | 4 个并发退款只成功 1 个；`refund_records` 只有 1 条；库存只回补一次 |
| 3 | 库存并发扣减 | 10 人抢 3 件 → 恰好 3 单成功、库存精确归零；`UPDATE stock = -1` 被 **CHECK 约束**拒绝（errno 3819） |
| 4 | 非法状态修改 | 未支付发货 / 未支付退款 / 已支付取消 / 重复支付 / 越权操作，全部被拒 |
| 5 | 支付与发货并发 | 绝不出现"未支付却已发货"，资金流水与订单状态一致 |

`test_resume_guard.py`（纯逻辑，9 项）逐条验证 `_resume_decision()` 的每个拒绝分支：
图已终态 / Checkpoint 为空 / 停在人工确认点 / `__interrupt__` / **`action_result` 存在
（execute_action 跑过，续跑会重复退款）** / `human_approved` 存在 / 开关关闭 /
读 Checkpoint 报错 —— 以及"只读节点跑过且图未结束"这一条允许续跑。

`test_side_effect_guard.py`（7 项）验证幂等键：首次认领成功 / 同一把键第二次被唯一约束挡住 /
重复请求能拿到上次结果快照 / 失败释放后同一次执行还能重试 / 新 `agent_run_id` 是新键 /
**10 个并发抢同一把键只有 1 个成功**。

`e2e_chain.py`（24 项）把整条链串起来验：
注册 → 建商品 → 加购下单（扣库存）→ 支付 → 售后 → 工单 → Agent（RAG + 过程事件）→
**真的退款成功**（订单 REFUNDED、恰好一笔流水、售后结单、库存回补、留下幂等键记录）→
工单结束且执行权释放 → 再点处理不会产生第二笔退款 → 客服工作台能看到完整过程。

`backend/scripts/acceptance.py`（业务链路验收，场景 A~D + Phase 19，含真实 LLM 与故障实例）
同样要跑，确认没有回归。

---

## 11. 老库怎么升级

`Base.metadata.create_all()` 只建缺失的**表**，不会给已存在的表加列。
项目不引 Alembic，所以启动时做一次轻量对齐（`app/core/schema.py`）：

```text
app/main.py lifespan → init_database() → create_all() → sync_schema(engine)
```

`sync_schema` 拿 SQLAlchemy metadata 当"模型真相"，和真实库结构做差集：

1. 缺列 → `ADD COLUMN`（先可空加列 → `UPDATE` 回填 → 收紧为 `NOT NULL`，兼容任何老数据状况）
2. 缺索引 / 唯一约束 → `CREATE INDEX` / `CREATE UNIQUE INDEX`
3. 只做"加列 / 加索引 / 对齐可空性"，**不做**改类型、删列（那是 Alembic 的活）

本次新增的字段：`tickets.agent_status`、`tickets.agent_run_id`、
`agent_runs.alive_at`、`agent_runs.retry_count`、`agent_runs.thread_id` 唯一约束、
`payments.active_order_id` / `refund_records.active_order_id`（生成列 + 唯一索引）、
`agent_runs.used_rag|used_tool|need_human` 的数据库默认值。

两个实现上的坑（都已处理，写在这里免得下次重踩）：

1. **生成列的 DDL 不能靠"反推建表语句"拼**：`_column_type()` 会 `replace(" NULL", "")`
   去掉可空性片段，而生成列的表达式里**合法地含有 `NULL` 字面量**
   （`CASE WHEN ... ELSE NULL END` 会被削成 `ELSE END` → 语法错误）。
   所以生成列走单独一条路 `_generated_column_definition()`，从模型定义直接拼，
   并且跳过后面的 `MODIFY COLUMN` 收紧（MySQL 的列定义顺序是
   `类型 AS (expr) STORED [NULL]`，我们拼的顺序会让它报语法错）。
2. **数据库默认值要单独对齐**：SQLAlchemy 的 `default=` 只是**Python 侧**默认值，
   绕过 ORM 直接 INSERT（测试造数据、运维手工订正）会撞 MySQL 严格模式的 1364。
   需要默认值的列列在 `_SERVER_DEFAULT_COLUMNS` 名单里，启动时幂等补 `SET DEFAULT`。

---

## 12. 业务并发安全（钱和货）

Agent 执行的互斥解决的是"同一工单别跑两次"，**资金与库存**则是另一组不变量。
钱的问题只用一层防护是不够的 —— 每一层都可能在某个分支上被绕过，所以同一件事做三层：

### 12.1 三层防护

```text
① 条件更新（主防线，Service 调用）        ← 检查与修改在同一条 SQL 内完成，拿 affected_rows 当判据
     UPDATE orders          SET payment_status='REFUNDED' WHERE id=? AND payment_status='PAID'
     UPDATE refund_records  SET status='SUCCESS'         WHERE id=? AND status='PENDING'
     UPDATE payments        SET status='SUCCESS'         WHERE id=? AND status='PENDING'

② 数据库唯一约束（物理兜底）              ← 前两层全被绕过也插不进第二条
     payments.active_order_id       GENERATED ALWAYS AS (
         CASE WHEN status IN ('PENDING','SUCCESS') THEN order_id ELSE NULL END) STORED
     + UNIQUE KEY uq_payments_active_order
     refund_records 同构（uq_refund_records_active_order）

③ 副作用幂等键（跨重试/续跑/并发）         ← 见 §12.3
     side_effect_claims.dedup_key UNIQUE
```

为什么退款服务里"先查一遍状态"还不够，非要用条件更新：
两个并发请求都可能在**加锁之前**读到同一个 `PAID`。行锁保证只有一个能进屋，
但"进屋之后状态是不是我要的那个"必须由 UPDATE 的 WHERE 来定 ——
拿到 0 行的请求必须**立刻停手**，不许再改订单 / 售后 / 库存 / 资金流水。

为什么唯一索引用**生成列**而不是普通 `UNIQUE(order_id)`：
失败的支付/退款尝试必须允许重复（用户重试是正常业务），
普通唯一索引会把"第一次支付失败"变成"永远不能再支付"。
生成列在状态不是有效状态时取 `NULL`，而 MySQL 唯一索引**不约束 NULL** ——
"有效支付至多一条、失败尝试不限条数"正好被一条索引表达出来。

### 12.2 各不变量落点

| 不变量 | 实现 |
|---|---|
| 一单只支付一次 | `payment_service.pay_order()`：`SELECT ... FOR UPDATE` 锁订单 → 校验状态 → `claim_success()` + `claim_paid()` 两道条件更新；重复请求 409 `ALREADY_PAID` |
| 一单只退一次 | `refund_service._execute()`：锁订单 + 事务内复查（按**已提交的最终状态**判，不只判 PENDING）+ `_claim_refund_or_die()` + `claim_refunded()`；订单那一步拿不到行就抛错回滚 |
| 副作用不重复执行 | `side_effect_claims` 表：`dedup_key` 唯一约束（见 §12.3） |
| 库存不超卖 | `product_repository.decrease_stock()`：单条 `UPDATE ... WHERE stock >= ?`，看 affected_rows；`products.stock` 上还有 `CHECK (stock >= 0)` 兜底 |
| 失败尝试不占位 | 生成列在 FAILED 时取 NULL（见上） |
| 状态机不被绕过 | 状态转换只写在 Service（Router 不碰状态）；每步都重新校验前置条件，非法转换 409 `INVALID_STATE` |
| 越权改别人的单 | 每个 Service 入口都校验 `order.user_id == 当前用户`（Agent 工具也走同一套） |

仓储层现在**只提供条件更新**：`set_paid()` / `mark_refunded()` / `mark_success()`
这几个"拿到 ORM 对象直接改字段"的方法已经把删掉了 —— 它们正是重复支付/退款的隐患，
只要有一个调用方忘了先查状态就会改出脏状态。

### 12.3 副作用幂等键（Tool 只负责参数转换，幂等由数据库保证）

```text
Agent → Tool → Service → Repository → MySQL
                  ↑
            这一层多一道"认领"：
            INSERT INTO side_effect_claims (dedup_key, ...)   ← 唯一约束
```

键的构成（`core/ids.py` 的 `gen_side_effect_key`）：

```text
refund:run:12:order:345     "第 12 次 Agent 执行对订单 345 的退款"
```

| 场景 | 结果 |
|---|---|
| 同一次执行内重试 / 从断点续跑 | 同一把键 → **第二次被唯一约束挡住**，不会重复动钱 |
| 恢复执行后再次走到退款 | 同上 |
| 并发请求同时打进来 | 只有一个能 INSERT 成功，其余拿到 1062 |
| 用户重新发起一次处理（新 AgentRun） | 新键 → 允许再走一遍业务规则（该不该退仍由 RefundService 判断） |

失败必须 `release()`（删掉认领），否则"一次失败就永远不能重试"；
成功则 `complete(result)` 存下结果快照，重复请求能如实转述"这件事已经做过、结果是……"。

这一层与"退款状态检查"**不重复**：业务规则管**该不该做**，幂等键管**做没做过**。

验收：`scripts/concurrency_safety.py`（25 项）+ `scripts/test_side_effect_guard.py`（7 项）
+ `tests/test_concurrency_money.py`（18 项，含**裸 SQL 插脏数据**验证唯一索引与 CHECK 真的生效）。

---

## 13. 代码索引

| 关注点 | 位置 |
|---|---|
| 执行权状态枚举 / 字段 | `app/models/ticket.py`（`AgentStatus`）、`app/models/agent_run.py`（`AgentRunStatus`） |
| 原子抢占 / 释放 SQL | `app/repositories/ticket_repository.py` |
| 抢占 / 释放 / 僵尸回收 / 接管（唯一入口） | `app/services/ticket_service.py` |
| 执行生命周期、重试、断点续跑、HITL 恢复 | `app/services/agent_service.py` |
| 节点级跟踪（`current_node`） | `app/agent/callbacks.py` |
| 心跳 | `app/agent/events.py`（`emit_event` 顺带刷 `alive_at`） |
| SSE 序号与断线续传（`Last-Event-ID`） | `app/agent/progress.py`、`app/api/tickets.py`（`stream_ticket`）、`frontend/src/api/tickets.ts` |
| 副作用幂等键 | `app/models/side_effect.py`、`app/services/side_effect_service.py`、`app/agent/nodes/action.py` |
| 支付/退款条件更新原语 | `app/repositories/payment_repository.py`（`claim_success`）、`refund_repository.py`（`claim_success`）、`order_repository.py`（`claim_paid` / `claim_refunded`） |
| 金额有效占位约束（生成列 + 唯一索引） | `app/models/payment.py`、`app/models/refund.py` |
| 库存原子扣减 / 约束 | `app/repositories/product_repository.py`、`app/models/product.py` |
| 知识发布 / 下线生命周期 | `app/services/knowledge_service.py`、`app/models/knowledge.py`、`app/rag/ingest.py` |
| 轻量 schema 升级（补列 / 补默认值 / 补生成列 / 补索引 / 补 CHECK / 枚举值回填） | `app/core/schema.py` |
| 确定性 LLM 桩 + 故障注入（仅测试） | `app/agent/stub_llm.py`、`app/api/test_hooks.py` |
| pytest 回归网（85 项，自拉实例） | `tests/conftest.py` + `tests/test_*.py` |
| 脚本式验收 | `scripts/concurrency_check.py`、`scripts/concurrency_safety.py`、`scripts/e2e_chain.py`、`scripts/test_resume_guard.py`、`scripts/test_side_effect_guard.py` |
