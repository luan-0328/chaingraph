# 智能电商工单平台（V2.0）

**FastAPI + MySQL + Qdrant + LangGraph + Vue 3** 的全链路 AI 工单系统：

> 用户下单/售后 → Agent 查**真实业务数据** + 检索**客服规则知识库** →
> 按代码里的规则自动处理，或转人工 → 客服批准后**恢复中断的 Graph** 继续执行退款 → 工单结单。

历史规格见 `v2.md`，当前后台任务与审批接口变化见 [Agent worker 运行说明](backend/docs/agent-worker.md)。
V1 骨架代码归档在 `legacy/`，仅作对照。

Agent 现在由独立 worker 执行：启动 API 后，另开终端在 `backend/` 运行
`uv run python -m app.worker`。仅启动 API 时任务会排队，不会自动运行。
旧的同步执行与审批验收脚本尚需适配新契约，以下历史测试数量不代表本轮验证结果。

```text
backend/    FastAPI + SQLAlchemy + Qdrant + LangGraph（app 116 个 py 文件 + tests 90 项 pytest 回归网）
frontend/   Vue 3 + TS + Element Plus 三端 SPA（52 个 ts/vue 文件）
legacy/     V1 骨架（旧 app/、旧前端 src、旧 sql）—— 不参与运行
v2.md       V2.0 规格书：数据模型 / 接口 / Agent 设计 / 验收标准
```

---

## 快速开始

```powershell
# ---------- 后端 ----------
cd backend
uv sync                    # 装依赖（uv 管理，声明在 pyproject.toml）
notepad .env               # 填 DATABASE_URL / LLM_API_KEY（见 .env.example）
uv run uvicorn app.main:app --reload
#   启动时自动：建库建表 → 灌演示数据 → 初始化 Checkpoint → 编译 Agent 图 →
#   知识库入库 → **向量索引自检**（索引与库不一致就从 MySQL 重灌，不重算 Embedding）

# ---------- 向量库（可选，但推荐）----------
# 不装也能跑：VECTOR_STORE 默认 qdrant，连不上时 RAG 自动回退到 MySQL 里的向量 + numpy 余弦，
# 并在 WARNING 日志与 /health 的 vector_store_active 里如实说明（不会假装在用 Qdrant）。
# 想要真正的向量库，起一个服务端（多实例/生产必须用这个模式）：
docker run -d --name qdrant -p 6333:6333 -v qdrant_storage:/qdrant/storage qdrant/qdrant
# 然后在 .env 里：QDRANT_URL=http://127.0.0.1:6333
# 留空 QDRANT_URL 则用**本地内嵌模式**（数据落 ./data/qdrant，零部署，但目录独占）

# ---------- 前端 ----------
cd frontend
npm install
npm run dev                # http://127.0.0.1:5173（/api 由 Vite 代理到 8000）
```

- 接口文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>（含 DB / LLM / Embedding / **向量库后端**状态）
- 演示账号（dev 环境自动创建，密码见括号）：`alice`(123456) 用户 · `service01`(123456) 客服 · `admin`(admin123) 管理员

### 一条命令验收（v2.md Phase 19 + Phase 20）

```powershell
cd backend

# ---------- pytest：并发安全 / 幂等 / 越权 / Agent 重试 / HITL / 向量库 / 完整链路（90 项）----------
uv run pytest                                   # 自己拉起被测实例（:8021 业务 / :8022 Agent 桩 / :8023 会真退款的链路）
uv run pytest tests/test_concurrency_money.py   # 只看"钱和货"的并发与幂等
uv run pytest -m agent                          # 只看 Agent 重试 / 断点续跑 / HITL

# ---------- 脚本式验收（黑盒 HTTP，输出人读的报告）----------
uv run python scripts/acceptance.py            # 业务链路：场景 A~D + 异常与安全（约 3~5 分钟，含真实 LLM 调用）
uv run python scripts/acceptance.py --only A   # 只跑"完整购物"
uv run python scripts/acceptance.py --only 19  # 只跑权限/归属/幂等/故障注入

uv run python scripts/concurrency_check.py     # 并发控制：同工单互斥 / 不同工单并发 / 断点恢复（66 项，不需要 LLM_API_KEY）
uv run python scripts/concurrency_check.py --only 5   # 只跑最核心的"并发竞争"
uv run python scripts/concurrency_safety.py   # 业务并发安全：重复支付/退款、库存超卖、非法状态（25 项，需 :8000）
uv run python scripts/e2e_chain.py            # 端到端链路：下单→支付→售后→工单→Agent→退款→结单（24 项）
uv run python scripts/test_resume_guard.py    # 断点续跑的安全判定（9 项纯逻辑，秒级）
uv run python scripts/test_side_effect_guard.py  # 副作用幂等键行为（7 项，含并发抢键）
```

**pytest 与脚本的分工**：脚本是**黑盒验收**（输出人读报告、自己拉故障实例、能打真实 LLM）；
pytest 是**可断言的回归网**，覆盖那些"必须失败才算通过"的路径（越权、非法状态、重复请求）。
两者共用同一批不变量，改代码后先跑 pytest 定位，再用脚本看整体报告。

`pytest` 会自己拉起三个被测实例（复用时不会抢占你手起的 `:8000`）：
`:8021` 纯业务实例、`:8022` 确定性 LLM 桩（并发/重试/HITL）、
`:8023` 确定性 LLM 桩 + `business_action=apply_refund`（**会真的把钱退掉**的完整链路）。
MySQL 不可达时整个 `integration` 组自动 skip，不会误报红。

### 注释与可读性体检

```powershell
uv run python scripts/check_comments.py          # 严格区不达标就退出码 1
uv run python scripts/check_comments.py --all    # 含历史区（只提示，不失败）
```

读源码是这个项目的一等公民，所以注释标准也被做成了可执行的检查，分三档：

| 规则 | 范围 | 说明 |
|---|---|---|
| 模块/类/公开函数都要有**中文** docstring | 严格区：`services/` `repositories/` `agent/` `rag/` + `core/exceptions.py` | 这些地方装着并发、事务、幂等、状态机的取舍；缺一句注释就可能被人删掉一个"看起来多余"的判断，然后线上重复退款 |
| 不允许整行**英文**注释 | 全项目 | 中文项目里突然冒出一句 `# check if ...` 是明显的风格断裂 |
| 关键文件必须含"为什么"级说明 | 全项目 | 见脚本里的 `REQUIRED_NOTES`：例如 `refund_service` 必须说明「条件更新」「幂等」的取舍 |

**为什么要分档**：`api/` `models/` `memory/` 那一批多是"一行转发"的薄封装，
缺的是 docstring 而不是理解成本 —— 它们被登记为待办（当前 127 处），
`--all` 能看到清单，但不阻塞。宁可把规则划窄一点、每条都真的做到，
也不要立一条"全项目 100%"却长期红灯的规则。

`acceptance.py` 脚本会自己拉起两个**故障实例**来做真实的故障注入（`--no-faults` 可关）：
`:8001` 用错误的 `LLM_API_KEY` 验证"模型挂了工单不卡死"，`:8002` 用不可达的 Embedding 地址验证"RAG 挂了降级回答"。

`concurrency_check.py` 会自己拉起一个**带确定性 LLM 桩**的实例（`:8010`），
把"模型调用"固定成 `sleep 1.2s` 以便稳定测出并发行为（详见 [`backend/docs/concurrency.md`](backend/docs/concurrency.md)）。

---

## 为什么这不是一个 demo

| # | 关键设计 | 具体落在哪 |
|---|---|---|
| 1 | **事务边界在 Service 层**：Repository 只 `flush()`，绝不 `commit()`；一次下单（扣库存 + 建订单 + 清购物车 + 写支付单）是一个事务，要么全成要么全滚 | `app/core/database.py` 的 `session_scope()`、`app/services/order_service.py` |
| 2 | **权限不靠前端**：前端隐藏按钮只是体验，后端每个接口都用 `require_roles()` 重新校验角色与数据归属 | `app/core/dependencies.py`、各 `app/api/*.py` |
| 3 | **Agent 不能自己乱来**：模型只输出受约束的结构化决策（`Literal` 枚举），走哪条边由**代码**判断；退款等动作再经 Service 规则校验，模型说"退 1000 元"没用 | `app/agent/schemas.py`、`app/agent/edges/conditions.py`、`app/services/refund_service.py` |
| 4 | **HITL 是真中断真恢复**：需要人工时图在 `interrupt()` 处停下并落 Checkpoint，客服批准后用 `Command(resume=...)` **从断点继续**（不是重跑一遍）；客服还可以**接管**（终止 Agent + 归属到自己） | `app/agent/nodes/human.py`、`app/memory/checkpoint.py`、`app/services/ticket_service.py` 的 `takeover_ticket()` |
| 5 | **过程可解释**：Agent 每一步业务事件（分析→检索→调工具→执行→等人工→完成）落库 `agent_run_events`，同时用 SSE 实时推给前端；断线重连带 `Last-Event-ID` 只补缺失的那一段 | `app/agent/events.py`、`app/agent/progress.py`、`app/api/tickets.py` |
| 6 | **RAG 有出处**：回答里带 `references`（标题 / 文件名 / 相似度），前端展示"参考知识"，检索不到就如实说没有 | `app/rag/*`、`knowledge/*.md` |
| 7 | **幂等与并发（三层防护）**：条件更新（`UPDATE ... WHERE status=可退款状态`）→ 数据库唯一约束（`payments/refund_records.active_order_id` 生成列）→ 副作用幂等键（`side_effect_claims`）。重复支付、重复退款、并发双击都只能成功一次（409 + 业务码） | `app/repositories/*_repository.py` 的 `claim_*()`、`app/models/payment.py`、`refund.py`、`side_effect.py` |
| 8 | **Agent 执行也互斥**：同一工单被并发点 N 次只会跑一次 Agent（原子 UPDATE 抢 `tickets.agent_status`，抢不到就返回当前那次执行）；不同工单之间没有任何全局锁，真正并发 | `app/repositories/ticket_repository.py`、`app/services/ticket_service.py`、[`docs/concurrency.md`](backend/docs/concurrency.md) |
| 9 | **失败可降级**：LLM 挂了 → 工单 FAILED 且不卡在 PROCESSING；RAG 挂了 → 无规则依据照样回答；Tool 出错 → 返回结构化错误给模型而不是抛穿 | `app/agent/nodes/analyze.py`、`retrieve.py`、`app/tools/*` |
| 10 | **知识有发布状态**：草稿不在向量库里（检索不到）；改正文自动退回草稿并作废旧向量；下线立刻不可检索但保留分块（重新发布不用重算向量） | `app/services/knowledge_service.py`、`app/models/knowledge.py`、`app/rag/ingest.py` |
| 11 | **向量库是"可换的索引"而不是"第二份数据"**：分块正文与向量始终以 MySQL 为准，Qdrant 只是它的检索索引 —— 换后端/索引丢了/换机器，一条命令从 MySQL 重灌（**不重算 Embedding、不花钱**）；Qdrant 连不上时自动回退 MySQL 全量余弦，RAG 不会因此瘫掉 | `app/rag/qdrant_store.py`、`app/rag/index.py`、`app/rag/vector_store.py` |

---

## 架构

```text
                    ┌──────────────── 前端（三端按角色分区路由）────────────────┐
                    │  /products /cart /orders /after-sales /tickets  （用户）  │
                    │  /customer-service/**                           （客服）  │
                    │  /admin/**                                      （管理）  │
                    └───────────────────────────┬──────────────────────────────┘
                                                │ HTTP + JWT + SSE
┌───────────────────────────────────────────────▼───────────────────────────────────┐
│ FastAPI Router            app/api/*.py          参数校验 + 权限，不写业务规则       │
│        ↓                                                                          │
│ Service                   app/services/*.py     ★ 业务规则 + 事务边界（唯一 commit）│
│        ↓                                                                          │
│ Repository                app/repositories/*.py 只拼 SQL / flush，返回 dict        │
│        ↓                                                                          │
│ MySQL（16 张表）           app/models/*.py       每个模型自带 to_dict()             │
└───────────────────────────────────────────────────────────────────────────────────┘

RAG 侧（两个存储，各司其职）：
    MySQL   knowledge_documents（文档本体）+ knowledge_chunks（分块正文 + 向量）← 真相源
    Qdrant  app/rag/qdrant_store.py   同一批分块的向量 + 检索元数据           ← 索引
            └─ app/rag/index.py       对账与重建：索引与库不一致就从 MySQL 重灌

Agent 侧（同样"业务规则在代码里"）：
    agent_service.process_ticket()  业务 ↔ Agent 的桥
        ↓
    LangGraph（app/agent/graph.py）
        analyze ──(决策)──► retrieve（RAG：Embedding → Qdrant Top K）──► tools（查订单/物流/售后）
           │                    │                   │
           └──────────────► action（执行退款等）◄────┘
                                │
                        need_human? ──是──► human（interrupt 挂起，落 Checkpoint）──► 客服批准 ► resume
                                │否
                                ▼
                             result（结构化结果 + 引用）──► 落库 + SSE 推送
```

**分层规矩**：Router 不碰 ORM，Repository 不管业务，Service 不拼 SQL，Agent 不绕过 Service 直接改库。
（Agent 的工具也只调 Service —— 和 HTTP 请求走同一套校验，没有"给 AI 开后门"的旁路。）

---

## 接口一览

全部业务接口需要 `Authorization: Bearer <token>`；错误统一返回 `{code, message}`（成功直接返回资源模型）。

<details>
<summary><b>用户端</b></summary>

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register`·`/login` | 注册 / 登录（JWT） |
| GET/PATCH | `/api/auth/me` | 当前用户 / 改资料 |
| GET | `/api/products`·`/products/{id}` | 商品列表（`limit/offset`）/ 详情 |
| GET/POST/PATCH/DELETE | `/api/cart`·`/cart/items` | 购物车：查看/加购/改数量/删除/清空 |
| POST | `/api/orders` | 结算下单（选中购物车行 → 扣库存 + 建单，**单事务**） |
| GET | `/api/orders`·`/orders/{id}`·`/orders/{id}/logistics` | 我的订单 / 详情 / 物流 |
| POST | `/api/orders/{id}/pay`·`/cancel`·`/confirm` | 模拟支付（幂等）/ 取消（仅未支付）/ 确认收货 |
| POST/GET | `/api/after-sales` | 申请售后 / 我的售后 |
| POST | `/api/after-sales/{id}/refund`·`/cancel` | **申请退款**（返回 `executed` / `need_human`）/ 撤销 |
| POST/GET | `/api/tickets` | 创建工单（带 `order_id`/`after_sale_id` 上下文）/ 我的工单 |
| POST | `/api/tickets/{id}/process` | **启动 Agent**（同步返回决策/结果/状态） |
| GET | `/api/tickets/{id}/stream` | **SSE**：Agent 业务进度（带历史回放 + `Last-Event-ID` 续传） |
| POST | `/api/tickets/{id}/messages`·`/close`·`/reopen` | 留言 / 关单 / 重开 |
| POST | `/api/tickets/{id}/approve`·`/reject`·`/takeover` | **HITL 三个出口**：批准（恢复 Graph）/ 驳回（转人工）/ 接管（终止 Agent + 领取归属）· 仅客服 |

</details>

<details>
<summary><b>客服端</b>（`CUSTOMER_SERVICE`）</summary>

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/customer-service/dashboard` | 工作台概览（待处理/待人工/今日退款…） |
| GET | `/api/customer-service/tickets`·`/{id}` | 全部工单（搜索/筛选/分页）/ 详情（含 Agent 过程） |
| POST | `/api/customer-service/tickets/{id}/assign` | 领取或指派（不传 `assignee_id` = 领取自己，传 `null` = 释放） |
| POST | `/api/customer-service/tickets/{id}/messages`·`/resolve`·`/close` | 回复 / 标记已解决 / 关闭 |
| GET | `/api/customer-service/orders`·`/{id}` | 订单列表 / 详情 |
| POST | `/api/customer-service/orders/{id}/ship`·`/transit`·`/deliver` | 发货 / 运输中 / 签收（模拟物流推进） |
| POST | `/api/customer-service/orders/{id}/refund/approve`·`/reject` | **批准/拒绝退款**（按订单维度） |
| GET | `/api/customer-service/after-sales`·`/refunds`·`/users` | 售后 / 退款流水 / 用户 |

</details>

<details>
<summary><b>管理端</b>（`ADMIN`）</summary>

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST/PATCH/DELETE | `/api/admin/products`… | 商品增删改查（含下架） |
| GET | `/api/admin/knowledge`·`/stats`·`/{id}` | 知识文档列表（可筛 `status`）/ 概览（含**向量库后端、点数、是否与库一致**）/ 详情，均带 `chunk_count` |
| POST/PATCH/DELETE | `/api/admin/knowledge`·`/{id}` | 新增（默认**草稿**）/ 修改（**改正文自动退回草稿**并作废旧向量）/ 删除（连带分块与索引点） |
| POST | `/api/admin/knowledge/{id}/publish` | **发布**：切分 → Embedding → 写入向量库，成功后 Agent 才能检索到 |
| POST | `/api/admin/knowledge/{id}/offline` | **下线**：Agent 立刻检索不到（分块与向量保留，重新发布不用重算） |
| POST | `/api/admin/knowledge/publish-drafts` | 一键发布全部草稿（批量导入后用） |
| POST | `/api/admin/knowledge/ingest` | 从 `knowledge/*.md` 批量入库（`force=true` **重算向量**；**只处理已发布文档**） |
| POST | `/api/admin/knowledge/reindex` | **重建向量索引**：只把库里的向量搬进 Qdrant，不重新调 Embedding（秒级、不花钱） |
| GET | `/api/admin/knowledge/search` | 检索调试（看得到分数与出处） |
| GET | `/api/admin/orders`·`/tickets`·`/users` | 订单 / 工单 / 用户总览 |

</details>

---

## 知识库生命周期（⑦ 知识管理）

```text
POST /admin/knowledge                    → DRAFT（草稿：不在向量库，Agent 检索不到）
        ↓ POST /admin/knowledge/{id}/publish        切分 → Embedding → 写入向量库
      ENABLED（已发布：RAG 能检索到）
        ↓ POST /admin/knowledge/{id}/offline
      DISABLED（已下线：分块保留，但状态过滤让它立刻检索不到）
        ↓ PATCH /admin/knowledge/{id} 改了 content
      DRAFT（草稿 + 旧分块/向量作废 + version 递增 → 必须重新发布）
```

两条硬规则：

1. **先算向量、成功之后才置 ENABLED** —— 反过来的话 Embedding 挂了会留下
   "状态是已发布、实际检索不到"的假发布（管理员以为上线了，Agent 永远查不到）。
   同理，**写向量索引失败也不会置 ENABLED**：发布整体失败、状态停在草稿，
   而不是"看起来发布了其实检索不到"。
2. **改了正文必须重算向量** —— 正文变了而向量还是旧的，检索命中的是**过时知识**，
   拿已经改掉的规则去回答用户比检索不到更危险。所以改正文自动退回草稿并删掉旧分块。

"能不能被检索到"只有一个开关：`knowledge_documents.status = 'ENABLED'`
（MySQL 版检索在 `knowledge_repository.list_chunks(enabled_only=True)` 的 JOIN 里过滤；
Qdrant 版先查出已发布文档 id、再作为 `document_id IN (...)` 下推给向量库）。
**向量库里不存在第二份发布状态** —— 所以下线、改稿、删除都不需要"记得同步向量库"，
也就没有"某条路径忘了过滤、把已下线的规则答给用户"的漏洞。

---

## 向量库（Qdrant）

```text
MySQL（真相源）                          Qdrant（索引，可随时重建）
knowledge_documents  文档正文与状态  ─┐
knowledge_chunks     分块正文 + 向量  ─┼─► app/rag/index.py ─► points：向量 + 检索元数据
                                      │      · 发布时跟着写（index_chunks）
                                      │      · 删文档/改正文时跟着删（drop_document_index）
                                      └──────· 不一致时从 MySQL 整体重灌（ensure_index_ready / reindex）
```

| 关注点 | 做法 |
|---|---|
| 检索 | `QdrantVectorStore.search()`：`query_points` + payload 过滤（已发布文档 id + 可选分类），Cosine 距离，`wait=True` 保证"刚发布就能检索到" |
| 写入 | 按**整篇重建**：先删这篇文档的点再写新的（分块序号会整体位移，增量写会混入旧正文的分块）；点 id 是 `(document_id, chunk_index)` 的确定性 UUID5，重发即覆盖 |
| 集合维度 | 用**实际向量长度**建集合，不信任 `EMBEDDING_DIM`；维度变了自动重建集合并告警（库里其余旧维度的分块需 `ingest?force=true` 重算） |
| 换模型过渡期 | 库里新旧维度混杂时，索引按**最新写入的那条向量的维度**建（它必然出自当前模型，而索引要服务的正是现在的查询向量），其余维度如实报在 `skipped_dim` 里 —— 比按历史多数派建要实用，否则新模型算出的查询一个都匹配不上 |
| 迁移 | **不需要重算 Embedding**：向量本来就在 MySQL 里，启动自检发现索引空/点数对不上就搬过去（几十毫秒） |
| 后端选择 | `VECTOR_STORE=qdrant`（默认）/ `mysql`；Qdrant 连不上（没起服务、本地目录被占、配置写错）→ WARNING + 自动回退 MySQL，`/health` 的 `vector_store_active` 与知识库概览都能看到**实际**在用什么 |
| 多实例 | 本地内嵌模式（`QDRANT_URL` 留空）目录**独占**，同一目录只能被一个进程打开；多实例/生产请起服务端（`docker run -p 6333:6333 qdrant/qdrant`）并把 `QDRANT_URL` 指过去 |
| 运维 | `POST /api/admin/knowledge/reindex` 重建索引（不重算向量）；`GET /api/admin/knowledge/stats` 看后端 / 点数 / `in_sync`；`check_comments.py` 与 pytest 都覆盖了这条路 |

---

## Agent 工具（Tool Calling）

工具**不让模型填 `user_id`/`ticket_id`**（从 State 注入），否则模型可以查别人的订单、退别人的款。

| 工具 | 作用 | 类型 |
|---|---|---|
| `query_order`·`query_order_items` | 查订单状态与明细 | 只读 |
| `query_logistics` | 查物流（含"多久没更新"） | 只读 |
| `query_after_sale`·`check_refund_eligibility` | 查售后 / 预判能不能退 | 只读 |
| `query_ticket_history`·`query_user`·`query_user_memory` | 历史工单 / 用户 / 长期记忆 | 只读 |
| `apply_refund` | **执行退款**（高风险） | 动作 |
| `create_after_sale` | 代用户创建售后单 | 动作 |
| `update_ticket` | 更新工单类型/优先级/标题 | 动作 |

## Agent 并发控制（同工单互斥 / 不同工单并发）

完整设计见 **[`backend/docs/concurrency.md`](backend/docs/concurrency.md)**，一页版本：

```text
POST /tickets/{id}/process
        ↓
UPDATE tickets SET agent_status='RUNNING', agent_run_id=?
 WHERE id=? AND agent_status='IDLE'          -- ← 原子抢占，看 affected_rows
        ├── 1 → 创建 AgentRun → LangGraph(async) → … → finally 释放执行权
        └── 0 → 不创建 AgentRun，返回当前活跃的那次执行
```

| 原则 | 落点 |
|---|---|
| 同一工单必须互斥 | `tickets.agent_status` 的原子 UPDATE（`app/repositories/ticket_repository.py`）；同工单被点 N 次只跑一次 |
| 不同工单可以并发 | 没有任何全局锁 —— "锁"就是工单自己那一行；`asyncio.Lock` 是明确禁止项 |
| LLM 异步、业务状态由数据库保证 | `await graph.ainvoke(...)` 解决 I/O 并发；一致性交给上面这条 UPDATE |
| 异常也必须释放 | `try/finally` 收尾，且带 `agent_run_id` 归属校验，防止误放别人的新执行 |
| 进程崩溃也不能卡死 | `agent_runs.alive_at` 心跳 + 僵尸执行回收（心跳超时的 RUNNING 可被接管） |
| 需要人工时不另起一次 | `WAITING_HUMAN` 只能由 approve/reject 恢复，并**恢复原 AgentRun 的原 thread_id** |
| 中途失败从断点续跑 | 可恢复异常重试时 `ainvoke(None, ...)` 复用同一 thread_id，已跑完的只读节点不重跑；动过业务就退回从头跑 |
| 失败可重试但要有边界 | 只对可恢复异常（超时/429/依赖抖动）重试，业务异常立刻失败并说明原因；重试是同一条 run，用户重新发起才是新 run |
| 钱和货同样有并发保护 | 支付/退款用行锁 + 事务内复查；库存用 `WHERE stock >= ?` 条件更新，外加 `CHECK (stock >= 0)` 兜底 |
| 副作用有幂等键 | `side_effect_claims.dedup_key` 唯一约束：同一次执行对同一订单的退款只可能落一次（重试/续跑/并发都被挡住），用户重新发起才是新的一次意图 |
| 过程可观测到节点级 | `agent_runs.current_node` 由 LangGraph 回调写入（"此刻卡在哪个节点"），配合 `alive_at` 心跳可区分"还在跑 LLM"和"进程已死" |

---

## 退款规则（"规则的唯一真相"在代码里）

`refund_service.evaluate_refund()` 依次判断，**硬性不满足直接抛业务异常，规则性风险转人工**：

| 判断 | 结果 |
|---|---|
| 订单存在 / 属于本人 | `ORDER_NOT_FOUND` / `PERMISSION_DENIED` |
| 已退款 | `ALREADY_REFUNDED` |
| 未支付 | `NOT_PAID`（无需退款） |
| 已有进行中的退款 | `DUPLICATE_REFUND`（幂等） |
| 已发货 / 已签收 | **转人工** `need_human=true` |
| 金额 > `REFUND_AUTO_APPROVE_LIMIT`（默认 500） | **转人工** `need_human=true` |
| 都不命中 | **自动执行退款**（`approved_by` 为空） |

人工批准走 `execute_approved_refund()`（跳过 `need_human` 判断，因为人已经批了），并记录审批人。

---

## 验收（v2.md Phase 19 / 20）

`backend/scripts/acceptance.py` 覆盖：

- **场景A 完整购物**：注册 → 登录 → 商品 → 购物车 → 下单 → 支付 → 发货 → 物流 → 收货 → 完成
- **场景B Agent 自动退款**：订单 → 售后 → 工单 → Agent 查订单 + RAG → 自动退款 → 工单结单
- **场景C Agent 规则问答**：问题 → RAG → 回答（不下单不退款）→ 用户确认 → 关单
- **场景D HITL 人工处理**：已发货的复杂售后 → Agent 判定转人工 → 客服批准 → 恢复 Graph → 退款（记审批人）→ 结单
- **Phase 19 异常与安全**：未登录 401 / 越权 403 / 跨用户数据隔离 / 重复支付 / 重复退款 / 非法状态转换（已支付取消、未发货收货、未支付发货）/ 重复加购合并 / **并发双击只成功一次** / Tool 失败返回结构化错误 / LLM 故障不卡单 / **结构化输出返回空时自动重试并给出可读错误** / RAG 故障降级 / 对未挂起工单批准返回业务错误

`backend/scripts/concurrency_check.py`（66 项断言，确定性 LLM 桩，不花 LLM 费用）覆盖：

| # | 测试 | 断言 |
|---|---|---|
| 一 | 同一工单 10 并发 | 只创建 1 个 AgentRun；1 个真执行 + 9 个返回 `ALREADY_RUNNING` |
| 二 | 10 张不同工单并发 | 10 个 AgentRun，总耗时 3.7s（串行需 24s）→ 证明没有被全局锁串起来 |
| 三 | Agent 抛异常 | AgentRun=FAILED 且带原因；`agent_status=IDLE`；工单不卡 PROCESSING；可重新发起 |
| 四 | WAITING_HUMAN | 再点处理不新建 Run；批准后**仍是同一条 AgentRun** 并推进到 COMPLETED |
| 五 | 并发竞争（核心验收） | 数据库里只有一个活跃 Run，恰好 1 个请求真的执行 |
| 六 | 断点恢复 | 注入"检索节点首次失败"：整体仍成功、`retry_count>=1`、`resumed=true`、**「正在分析工单」只出现一次** |
| 附加 | 重试 / 僵尸执行 | 可恢复异常重试一次即成功；心跳超时的 RUNNING 能被回收，不会永久卡死 |

`backend/scripts/concurrency_safety.py`（25 项断言）覆盖**钱和货**的不变量：
并发支付只成功一次、并发退款只产生一笔流水、10 人抢 3 件库存不超卖
（`UPDATE stock = -1` 会被 DB 的 CHECK 约束拒绝）、未支付发货/已支付取消等非法状态全被拒、
支付与发货并发绝不出现"未支付却已发货"。

`backend/scripts/e2e_chain.py`（24 项断言）把整条链串起来验一遍：
注册 → 建商品 → 加购下单（扣库存）→ 支付 → 售后 → 工单 → Agent（RAG + 过程事件）→
**真的退款成功**（订单 REFUNDED、恰好一笔流水、售后结单、库存回补、留下幂等键记录）→
工单结束且执行权释放 → 再点处理不会产生第二笔退款 → 客服能看到完整过程。
默认跑确定性 LLM 桩（不花钱），`--with-llm` 换成真实模型。

前端另有一套浏览器端 E2E（Playwright，真实点按钮 + 真实 LLM）：
`cd frontend && node e2e-v2.mjs`（需后端 :8000 与前端 :5173 都已启动）。

### pytest 回归网（`backend/tests/`，90 项）

`uv run pytest` 会把被测服务**自己拉起来**（和生产的启动方式完全一致），
只用确定性 LLM 桩替换模型调用，所以**不花 LLM 费用、也不需要你手动先起服务**。

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `test_concurrency_money.py` | 18 | 并发支付/退款只成功一次且只产生一笔流水、库存只回补一次、串行重放被业务码挡住、**数据库唯一索引与 CHECK 约束真的生效**（裸 SQL 插脏数据验证）、FAILED 尝试不占位、条件更新原语 |
| `test_permissions.py` | 25 | 未登录 401 / 角色越界 403 / 跨用户订单·工单·购物车隔离、**Tool 层没有后门**（直接调 Tool 也退不了越权的钱、超额的款）、非法状态转换、金额上下限 |
| `test_agent_recovery.py` | 15 | 可恢复异常→断点续跑（已完成的节点不重跑）、不可恢复异常不重试、失败也释放执行权、失败原因落库、重新发起是新 run、僵尸执行回收、同工单并发只建一条 run、不同工单真并发、事件序列与 `current_node` |
| `test_hitl.py` | 19 | 进入等待人工（Checkpoint/状态绑定）、等待期间不新建 run、批准恢复**原 thread_id 原图**、驳回不退款、**接管**终止 Agent + 归属、连点与并发的幂等/互斥 |
| `test_e2e_chain.py` | 8 | 完整链路：下单→支付→售后→工单→Agent→**真退款**→结单（订单/流水/售后/库存/幂等键逐项断言）、HITL 批准后真退款并记录审批人、重复处理不退第二次、**知识库发布/下线对 RAG 的实际影响** |
| `test_vector_store_qdrant.py` | 5 | **Qdrant 这条路本身**：发布→索引有点→检索命中（分数/出处/分类都对）、分类过滤真的下推、下线立刻检索不到但分块保留、删文档连索引点一起清、索引被清空后启动自检能从 MySQL 灌回来、维度不一致降级为空 |

`test_vector_store_qdrant.py` 为什么单独存在：本地内嵌模式的 Qdrant 目录是**独占**的，
pytest 会同时拉起 3 个实例，于是只有第一个用得上 Qdrant（其余按设计回退 MySQL）——
"Qdrant 实现到底对不对"必须在进程内单独打它，否则换了后端可能一行都没被跑到。
它用**临时目录 + 独立集合**，不碰你开发机上的真实索引，也不发 Embedding 请求。

跑测试的先决条件只有一条：MySQL 可达。连不上时整组自动 skip 并说明原因，不会误报红。

**测试数据会留在库里**（明确取舍）：这些用例是**真写库**的（要的正是真实事务与唯一约束），
每跑一轮约新增 90 个 `pytest_*` 账号及其订单/退款/工单。会话结束时会打印新增量，
想清理时执行：

```powershell
uv run python scripts/cleanup_test_data.py            # 先预览会删什么（默认 dry-run）
uv run python scripts/cleanup_test_data.py --apply    # 真的删（只删 pytest_* 名下的数据）
```

---

## 并发与幂等：三层防护（② 订单/支付/退款并发安全）

钱和货的问题只用一层防护是不够的 —— 每一层都可能在某个分支上被绕过。所以同一件事做三层：

```text
① 条件更新（主防线，Service 调用）
     UPDATE orders          SET payment_status='REFUNDED' WHERE id=? AND payment_status='PAID'
     UPDATE refund_records  SET status='SUCCESS'         WHERE id=? AND status='PENDING'
     UPDATE payments        SET status='SUCCESS'         WHERE id=? AND status='PENDING'
   检查与修改在**同一条 SQL** 里完成，没有"先查后改"的竞态窗口；以 affected_rows 为准。

② 数据库唯一约束（物理兜底）
     payments.active_order_id        GENERATED ALWAYS AS (
         CASE WHEN status IN ('PENDING','SUCCESS') THEN order_id ELSE NULL END) STORED
     + UNIQUE KEY uq_payments_active_order
     refund_records 同构（uq_refund_records_active_order）
   一个订单至多一笔"有效"支付/退款。FAILED 不占位（取 NULL）——
   否则"第一次支付失败"会变成"永远不能支付"。唯一索引不约束 NULL，正好表达这个语义。

③ 副作用幂等键（跨重试/续跑/并发）
     side_effect_claims.dedup_key UNIQUE     例：refund:run:12:order:345
   键里带 agent_run_id，所以"同一次执行内的重试/断点续跑"只做一次，
   而"用户重新发起"是新意图 → 新键 → 重新走一遍业务规则。
```

`payments` / `refund_records` 的生成列由 `core/schema.py` 在启动时自动补到已有库上
（幂等，`ALTER TABLE ... ADD COLUMN ... GENERATED ALWAYS AS (...) STORED` + `CREATE UNIQUE INDEX`）。

---

## 演示数据（dev 环境自动写入，幂等）

4 个账号 / 8 件商品（含 1 件下架、1 件缺货）/ 7 篇客服规则文档（57 个分块）。

| 账号 | 角色 | 说明 |
|---|---|---|
| `alice` / `123456` | USER | 普通用户：下单、售后、提工单 |
| `bob` / `123456` | USER | 第二个用户：验证跨用户数据隔离 |
| `service01` / `123456` | CUSTOMER_SERVICE | 客服：工单处理、发货、批准退款 |
| `admin` / `admin123` | ADMIN | 管理员：商品、知识库、全量数据 |

---

## 已知取舍 / 待办

1. **Embedding 默认是本地降级向量器**（512 维字符 n-gram 哈希）——不配 `EMBEDDING_API_KEY` 也能跑通 RAG 链路，
   但语义相似度弱（命中分数 0.2~0.4，且更偏词法重合）。配好真实 Embedding 后执行
   `POST /api/admin/knowledge/ingest?force=true` **重算向量**即可，**不需要改代码**。
   ⚠️ 换 embedding 模型后一定要做这一步：库里会同时存在旧维度与新维度的向量，
   一个向量索引只能有一个维度，此时检索会记一条"维度不一致"的 warning 并返回空
   （比拿两个模型的向量算相似度要好 —— 那种分数毫无意义）。
2. **支付/物流是模拟的**：`/pay` 直接把订单置为 PAID，物流状态由客服端接口推进（没有接真实支付/快递网关）。
3. **Checkpoint 落在 SQLite**（`backend/data/checkpoints.sqlite`）：单机够用，多实例部署要换成 Postgres/Redis Saver。
4. **用户长期记忆**（`user_memories`）只做了落库与查询工具，还没有"自动总结历史对话写回记忆"的环节。
5. **SSE 是进程内内存总线**：多实例部署时事件不互通，要换 Redis Pub/Sub
   （`emit_event()` 的调用方不用改；事件本身已经落库，所以换掉只影响实时性，不影响可追溯性）。
6. **向量库默认 Qdrant，但"本地内嵌模式"的目录是独占的**：`QDRANT_URL` 留空时数据落在
   `backend/data/qdrant`，同一目录只能被一个进程打开 —— 开发单实例足够，
   pytest 同时拉 3 个实例时只有第一个用得上 Qdrant（其余 WARNING + 回退 MySQL，测试照样通过）。
   多实例/生产请起服务端（`docker run -p 6333:6333 qdrant/qdrant`）+ `QDRANT_URL`，
   那时 payload 索引（`document_id` / `category`）才真的生效（本地模式会自动跳过建索引）。
   另外：检索时的"已发布文档"过滤是**查询时从 MySQL 取**的（见 rag/qdrant_store.py），
   知识文档上千篇以后应把 status 放进 payload 并同步维护 —— 那时"唯一开关"的位置也会跟着挪。
7. **没有物理外键**：表之间靠 Service 保证一致性（`ON DELETE` 的级联由 Service 显式完成，
   例如删知识文档会连带删分块与索引点）。这样便于分库与压测，但绕过 Service 直接改库要自己负责。
8. **金额用 `Decimal` + `Numeric(10,2)`，但接口层是 `float`**：当前金额量级下无碍，
   真要严谨到分，DTO 层也应该换成字符串或整数分。
