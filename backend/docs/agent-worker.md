# 售后审批与后台 Agent 任务

本轮实现范围：全额退款业务闭环、持久化任务、单机独立 worker。
未接入 PostgreSQL Checkpointer、消息队列或多机器 worker；支付仍是模拟业务。

## 启动

在 backend 目录分别启动两个进程：

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
uv run python -m app.worker
```

API 负责提交、审批和事件读取，不打开 Checkpoint。worker 独占图与 SQLite
Checkpoint。两个进程使用相同 DATABASE_URL 和配置。保留 data 目录，尤其不能
删除 checkpoints.sqlite 后试图恢复旧任务。

建议 Qdrant 使用独立服务并配置 QDRANT_URL；本地内嵌模式有目录独占限制，
API 和 worker 同时使用时可能回退到 MySQL 向量检索。也可以显式设置
VECTOR_STORE=mysql，但这仍是小知识库的全量检索方案。

新增配置（默认值）：

```dotenv
AGENT_LEASE_SECONDS=120
AGENT_HEARTBEAT_SECONDS=15
AGENT_POLL_SECONDS=1
```

心跳间隔必须小于租约时长的一半。worker 应由进程管理器托管，异常退出后重启。
worker 本身不会在退出后复活。恢复扫描发生在 worker 启动和运行期间，而不是依赖
用户再次点击；等待人工审批不会作为运行超时回收。

## 状态与事务

- API 在一个短事务内锁工单、检查当前任务、创建 AgentRun、绑定工单指针。
- `agent_runs.status` 是任务生命周期的依据。`tickets.agent_status` 仅保留为
  旧页面与 DTO 的兼容投影，新的领取逻辑不依赖它。
- worker 领取 PENDING 或租约到期的 RUNNING，递增 execution_epoch，设置
  worker_id、lease_until。WAITING_HUMAN 不参与任务领取。
- 每个 worker 业务事务通过 ExecutionClaim 校验工单关联、Run 状态、代次、
  worker_id 和租约，按“工单 → Run → 订单”顺序持锁到提交。嵌套服务共用该事务。
- 独立线程续租；无法确认续租成功时 worker 退出，避免仅取消协程却留下工具线程。
- 同机进程文件锁覆盖 worker 的整个生命周期，防止旧进程与新进程同时写
  Checkpoint。这不是跨机器 fencing；当前只支持在同一主机、同一 checkout 下运行一个 worker。

## 审批与恢复

工单审批请求必须携带 expected_run_id 和 review_round。审批人取登录账号，
不能由前端指定。review_payload 保存方案和订单、售后状态快照。

售后页先获取售后详情中的 approval_context，提交 after_sale_id、预期 Run、
轮次和 order_snapshot。有关联活动 Agent 时提交到原任务；没有活动 Agent 时，
在短事务内完成直接退款和明确关联售后的工单同步。多个活动工单关联同一订单时，
要求客服到工单页分别核实，不能随意选择一个方案。

审批决定先落 MySQL，然后任务回到 PENDING。worker 用原 thread_id 恢复
interrupt；重复提交同一轮同方向审批不会反转决定。审批前状态变化会更新审核版本，
要求刷新确认；审批后、真正退款前再次持订单锁复查，变化则重新进入审核。

恢复读取 Checkpoint：

1. 有人工中断、没有审批：补记 WAITING_HUMAN。
2. 有人工中断、有审批：Command(resume=决定)。
3. 有剩余节点：ainvoke(None) 续跑。
4. 图已完成：仅补任务收尾。
5. 恢复任务缺少 Checkpoint：失败并交人工核实，禁止猜测进度从头执行。

退款已提交但幂等结果快照未写入时，先查询退款流水和订单状态；只有确认退款已完成，
才复用成功结果。认领存在但业务结果仍未知时转人工，不盲目重新退款。

## 业务范围与接口变化

- 仅开放 REFUND 全额退款，退货、换货通过普通工单人工受理。
- 模型必须区分 request_kind（咨询/动作/未知）与 resolution_kind
  （答复/动作/缺信息/人工）；缺少有效方案不能自动结单。
- POST /tickets/{id}/process 返回 202 和任务状态，不再等待执行完成。
- 工单批准/驳回为异步提交；返回工单不代表退款已经完成。
- SSE 从 MySQL 读取持久化事件，用事件主键作为游标，不再依赖进程内总线。
- 排队或运行中的任务禁止手动结单、关闭或强行接管；等待人工的任务可以事务性接管。

## 升级与验证边界

先停止旧 API 和 worker，再启动新版 API 完成现有 sync_schema 的加列、加索引。
新增字段均有默认值或允许 NULL；本轮没有删除旧数据或更换 Checkpoint 文件。
旧的等待审核任务若没有 review_payload，需要人工接管，不能沿用无法核对的旧审批。
上线前应备份 MySQL 与 Checkpoint，并使用同一版本部署 API、worker 和前端。

本轮按用户要求未运行测试、未启动服务、未执行数据库升级。只做 Python 语法解析
和 Vue/TypeScript 静态类型检查。已有同步执行测试和旧审批请求参数需要适配新的
异步契约，不能直接沿用历史通过数量。下一轮应重点验证并发提交、重复审批、租约
失效、退款后崩溃、审批后发货、SSE 重连，以及存量 schema 升级。
