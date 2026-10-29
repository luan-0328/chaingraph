# 智能电商客服与售后工单平台 —— 前端（V2.0）

Vue 3 + TypeScript + Vite + Element Plus + Pinia + Vue Router + Axios + SSE。

按 `../v2.md` 实现：**这是一个轻量电商 + 客服工单系统，Agent 深度嵌入售后处理流程**。
不是 AI Chatbot —— 没有聊天式首页、没有粒子/霓虹装饰，也不展示模型思维链。

## 三套界面（按角色）

| 角色 | 路由前缀 | 页面 |
|---|---|---|
| `USER` 普通用户 | `/` | 商品列表/详情、购物车、我的订单、订单详情（支付/物流/确认收货/申请售后）、我的售后（申请退款）、我的工单（对话 + Agent 处理进度）、个人信息 |
| `CUSTOMER_SERVICE` 客服 | `/customer-service` | 工作台数据概览、工单处理（筛选/领取/回复/审批/解决/关闭）、售后审核、订单查询（发货/运输/签收）、用户查询 |
| `ADMIN` 管理员 | `/admin` | 商品管理（CRUD/上下架）、订单总览、工单总览、用户总览、知识库管理（入库 + 检索调试） |

登录/注册 `/login`、`/register`（公开）。路由守卫按角色分流，**但真正的权限由后端 `require_roles` 再校验一次**（v2.md §58）。

## 运行

前提：后端已启动（`cd ../backend && uv run uvicorn app.main:app --reload`，监听 8000）。

```powershell
npm install
npm run dev        # http://127.0.0.1:5173
```

演示账号（后端 dev 环境自动创建，登录页可点击直接填入）：

| 角色 | 账号 | 密码 |
|---|---|---|
| 普通用户 | `alice` | `123456` |
| 客服 | `service01` | `123456` |
| 管理员 | `admin` | `admin123` |

其他命令：`npm run build`（类型检查 + 打包）、`npm run type-check`、`npm run lint`、`npm run format`。

开发时 `/api` 由 Vite 代理到 `http://127.0.0.1:8000`（见 `vite.config.ts`），所以浏览器看到的是同源请求，不涉及 CORS。

## 目录结构

```text
src/
├── api/          auth / products / cart / orders / afterSales / tickets(含 SSE) / customerService / admin
├── components/
│   ├── common/     StatusTag（枚举→标签）、UserMenu
│   ├── product/    ProductCard
│   ├── order/      OrderStatusTags（三个状态维度）、OrderItemsTable
│   ├── ticket/     TicketMessageList、TicketMessageInput
│   └── agent/      AgentProcessTimeline（业务事件时间线）、AgentResultCard、ApprovalDialog（HITL 审批）
├── layouts/      UserLayout（顶部导航）、CustomerServiceLayout（侧边菜单，客服/管理共用）
├── router/       路由表 + 角色守卫
├── stores/       auth（token / 用户 / 购物车角标）
├── types/        index.ts —— **与后端 app/schemas 逐字段对应的唯一契约**
├── utils/        request(axios 封装) / storage / dict(枚举文案) / format
└── views/        Login / Register / Profile + user/ + customer-service/ + admin/
```

## 关键实现点

### 1. 前端不发明字段
`src/types/index.ts` 与后端 `app/schemas/*.py` 一一对应；中文文案集中在 `src/utils/dict.ts`。
后端改字段 → 两边一起改，前端不自己造枚举（v2.md §83 规则 4/5）。

### 2. 身份来自 JWT，不由前端传
创建订单/工单的请求体里**没有 user_id**；后端从 token 取。前端也拿不到别人的数据（越权后端返回 403）。

### 3. SSE 用 fetch 而不是 EventSource
`EventSource` 不能自定义请求头，没法带 `Authorization`（只能塞 URL，会进日志）。
`api/tickets.ts#subscribeTicketStream` 用 `fetch` + `ReadableStream` 自己解析 SSE，能带鉴权头、能 abort。
后端建流时会**回放历史事件**，所以晚连上/断线重连都不会漏进度。

### 4. 断线恢复 / 刷新恢复
- 断线 ≠ 失败：先 `GET /tickets/{id}` 查真实状态，仍为 `PROCESSING` 才退避重连；
- 详情页挂载时若状态是 `PROCESSING`（说明有 Agent 在跑），自动重连 SSE。

### 5. HITL 审批是"恢复图执行"
客服在工单详情点"人工审批"→ `POST /tickets/{id}/approve` → 后端用 `Command(resume=...)` **恢复被 `interrupt()` 暂停的 LangGraph 图**，继续执行退款并关单。
驳回则结束本次 Agent 流程，工单转客服人工处理。

### 6. 错误处理
`src/utils/request.ts` 统一处理：401 清登录态回登录页、403/404/409/422/500/网络异常各给人话提示，
并优先展示后端返回的 `message`（后端已经写好中文业务提示）。异常堆栈永不展示给用户。

## 已知取舍

- 没有单元测试 / CI。
- Element Plus 全量引入（未做按需加载优化）。
- 用户端与管理端/客服端共用一套 Element Plus 主题，只做了浅色企业风格，没做移动端适配（v2.md 定位是后台系统）。
