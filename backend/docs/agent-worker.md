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
AGENT_MAX_CONCURRENCY=4
AGENT_REVIEW_REMINDER_SECONDS=3600
AGENT_REVIEW_TIMEOUT_SECONDS=86400
```

心跳间隔必须小于租约时长的一半。worker 应由进程管理器托管，异常退出后重启。
worker 本身不会在退出后复活。恢复扫描发生在 worker 启动和运行期间，而不是依赖
用户再次点击；等待人工审批不会作为执行租约超时回收，而是使用独立的审批期限。

### 心跳失败与租约到期

`alive_at` 记录最近活跃时间，`lease_until` 是数据库中的执行权截止时间。
独立线程每次先校验 `worker_id`、`execution_epoch`、任务关联与有效租约，
再更新 `alive_at` 并把 `lease_until` 延长一个租约周期。已经过期的执行不能自行续租。

心跳失败不等于租约到期：数据库断连、事务失败、执行者或代次变化也会导致续租失败。
即使截止时间还没到，无法确认续租成功时当前实现也会以退出码 2 结束整个 worker，
同进程其他工单也会中断。只取消协程无法保证同步工具线程停止，因此保留这一保守策略。
重新启动后仍按数据库中的原租约等待到期，再领取并递增执行代次；不会因为重启就抢占有效租约。

生产运行需由 systemd 等进程管理器托管。例如服务配置中的关键项为：

```ini
[Service]
WorkingDirectory=/absolute/path/to/chaingraph/backend
ExecStart=/absolute/path/to/chaingraph/backend/.venv/bin/python -m app.worker
Restart=on-failure
RestartSec=3
KillMode=control-group
```

以上仅为部署示例，需要填写实际路径、运行账号并配置日志告警；仓库测试不会安装或启动系统服务。
默认 120 秒租约下，故障恢复可能要等待剩余租约时间，不能用测试中的 6 秒租约宣称生产秒级恢复。

### 异步节点与同步事务

同步 SQLAlchemy 继续负责事务。异步节点使用 `aemit_event()` 在线程中完成事件落库，
审核方案保存、首次工单读取与业务工具同样通过 `asyncio.to_thread()` 调用。
每个完整同步事务在自己的线程内创建并关闭 Session，不跨线程传递正在使用的 Session。
`to_thread` 传播执行凭证的 ContextVar；不表示同步函数可被协程取消强制终止。
新增调度回归会模拟事务等待，要求另一协程能推进，并核对线程收到同一执行凭证。

## SQLite 与工单并发

保持单机单 worker。worker 内最多同时执行 `AGENT_MAX_CONCURRENCY` 张不同工单，
默认 4、允许 1～32；同一工单仍由行锁、活动任务复用和执行租约保证单次执行。
每个并发任务独立携带执行凭证、心跳和 `thread_id`，不能把多工单改成共用一个 thread。

所有任务共享一个 `AsyncSqliteSaver`。Saver 自身的 `asyncio.Lock` 串行化读写操作，
不会持锁等待整次模型调用；SQLite 使用 WAL、5 秒忙等待和 FULL 同步。
进程生命周期同时持有业务库锁和实际 SQLite 文件旁的 `.worker.lock`：同库不同
Checkpoint 路径、不同连接标识共用同一 Checkpoint 文件均会拒绝第二个 worker。
锁文件存在不代表锁仍被持有，进程退出由操作系统释放；运行中不能删除锁文件。

这些措施不支持跨机器、多 checkout 使用不同 Checkpoint 的多 worker 部署。
不要把本地 SQLite 放到网络文件系统并宣称已获得分布式互斥。

## 版本与升级边界

任务提交时保存 `flow_version` 和 `execution_spec`，包含后端代码、Prompt、关键
LangGraph 依赖版本，以及不含密钥的模型、Embedding 和核心业务参数。初始图状态
也保存版本；worker 在读取和恢复状态前核对任务版本，恢复时核对 Checkpoint 的
任务归属和版本。更换 API Key 不影响版本指纹。

API 与 worker 必须使用同一套代码和执行配置。版本不兼容的排队任务会失败并保留
审计，等待审核的旧方案不能批准或驳回；客服页面显示原因并允许明确人工接管。
缺少版本的历史任务不会自动标记成新版。这里提供的是保守拒绝不兼容恢复，
没有自动加载历史代码、多版本路由或 Checkpoint 数据迁移功能。需要继续旧流程时，
应在升级前处理完旧任务，或在兼容环境中核实处理，不能手工改指纹绕过检查。

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

## 审批待办运营

每轮审核在保存方案时固定申请时间和截止时间；重放中断或重复提交不会延长期限。
订单快照变化产生新审核轮次时重新计时。默认 1 小时后写一条站内提醒，24 小时
到期；提醒时间必须小于过期时间。worker 即使所有执行槽都被占用，也会继续扫描
审批期限。重启后会补扫未处理的提醒及过期方案。

提醒写入工单系统消息和事件表，同一轮只写一次。到期后结束自动任务，保留原方案
与 Checkpoint，把工单交回客服跟进；不自动批准、退款、驳回售后申请或关闭工单。
HTTP 审批接口也会独立检查期限，所以 worker 暂时停机时不能批准过期方案。
审批、到期扫描和人工接管按相同锁顺序互斥。

客服工作台与等待审核的详情页每 15 秒刷新，详情显示截止时间或不可审批原因，
并提供人工接管入口。目前提醒渠道为站内消息，没有发送邮件、短信或外部通知。

## 用户转人工与人工处理模式

人工审批与人工交流分开。`handling_mode` 有三个值：`AGENT` 允许自动处理，
`HUMAN_PENDING` 表示已请求交接但当前节点尚未退出，`HUMAN` 表示后续由客服处理。
用户按钮调用 `POST /api/tickets/{id}/request-human`，必须拥有该工单。重复请求复用
同一轮交接，不重复创建任务或系统交接消息。

创建工单和发送消息时先规范化文本，对独立短指令做 `fullmatch`，如“转人工”
“我要人工客服”“请帮我转接人工客服”。“不要转人工”“人工客服几点上班”及引用、
条件句不走此捷径。规则只调用固定的交接服务，不从用户文本动态查找方法，更不
通过匹配文本批准退款。复杂表达由结构化 `request_human` 字段识别后进入同一服务。

没有任务、排队任务或暂停审核的任务可以立即进入人工待办。执行中的任务保留
原执行资格和心跳，只设置交接请求；节点前后检查和业务事务入口会阻止后续自动
写入。worker 等当前模型或工具调用返回，确认图已退出后完成交接，不强杀整个
worker、不取消后留下工具线程继续执行，也不会停止其他工单。

若进程在交接期间崩溃，新 worker 会在租约到期后优先完成交接，不需要猜测或恢复
业务步骤。已经提交的退款不能被撤销；系统会核对订单退款状态并在交接消息中提示
客服核实。原 Checkpoint 与历史执行保留，但交回后不会复活该旧任务。

Agent 无有效方案、缺少必要事实或规则、无法自动执行动作，或者最终执行失败时，
进入人工处理队列。正常的“有可执行方案，但需批准”仍走人工审批。交接使原任务
终止，`HUMAN_HANDOFF` 事件和执行原因区分主动转接与退款申请被驳回；不会擅自
拒绝售后申请。

客服领取后可在原工单内回复，双方页面每 5 秒刷新人工对话。人工模式禁止启动新
Agent，也禁止恢复旧审批；只有负责客服通过 `POST /api/tickets/{id}/return-to-agent`
明确交回后才解除限制。解除限制不立即执行动作，再次启动会创建新的 Run/thread。
交接未确认前，领取、手工结单等操作仍受活动任务保护。

## 业务范围与接口变化

- 仅开放 REFUND 全额退款，退货、换货通过普通工单人工受理。
- 模型必须区分 request_kind（咨询/动作/未知）与 resolution_kind
  （答复/动作/缺信息/人工）；缺少有效方案不能自动结单。
- POST /tickets/{id}/process 返回 202 和任务状态，不再等待执行完成。
- 工单批准/驳回为异步提交；返回工单不代表退款已经完成。
- SSE 从 MySQL 读取持久化事件，用事件主键作为游标，不再依赖进程内总线。
- 排队或运行中的任务禁止手动结单、关闭或强行接管；等待人工的任务可以事务性接管。
- 用户可以请求运行中任务安全转人工；“交接请求”与“交接已完成”分别展示。

## 升级与验证边界

先停止旧 API 和 worker，再启动新版 API 完成现有 sync_schema 的加列、加索引。
新增字段均有默认值或允许 NULL；本轮没有删除旧数据或更换 Checkpoint 文件。
旧的等待审核任务若没有 review_payload，需要人工接管，不能沿用无法核对的旧审批。
上线前应备份 MySQL 与 Checkpoint，并使用同一版本部署 API、worker 和前端。

### 当前异步版本的回归测试

在 backend 目录运行：

```bash
uv run python scripts/run_async_regression.py
# 仅复查崩溃恢复：其余参数直接传给 pytest
uv run python scripts/run_async_regression.py -k crash_windows
# 前端依赖安装后，可额外运行真实 Chrome 审批页面测试：
uv run python scripts/run_async_regression.py --browser -k browser_review
```

需要 MySQL 可连接，且配置的账号具有创建和删除测试库的权限。启动器每次生成
独立的 `agent_async_test_<随机串>` 数据库、临时日志与 Checkpoint，自动启动真实
API 和 worker 子进程。完成后停止子进程、删除测试数据库、临时目录及 worker 锁。
原业务库、`.env` 和原 Checkpoint 不参与测试。API 使用空闲端口，不复用已有服务。

新用例在 `tests_async/`，不依赖旧 `tests/conftest.py` 的同步返回和进程内测试钩子。
直接运行这些用例而没有隔离启动器标识会报错，防止误写业务库。

覆盖异步 202 与任务持久化、并发提交复用、归属和审批权限、活动任务状态保护、
自动退款及库存/流水/消息一致性、审核版本变化、重复审批与驳回、人工接管、SSE
游标回放、临时故障重试、不可恢复故障、worker 互斥与旧租约凭证失效。

崩溃恢复用例会直接终止 worker 进程，并等待真实租约到期后重新启动。注入窗口包括：
退款事务提交但幂等结果尚未保存、动作完成但图尚未保存、图完成但任务尚未收尾。
恢复后同时断言退款流水仅一笔、库存只回补一次、最终回复只写一次、工单状态正确，
并验证用户主动重开的工单不会被旧任务重新结单。

模型输出使用确定性桩，HTTP、LangGraph、MySQL、SQLite、Service 和 Tool 均运行
真实实现；这套回归不调用付费模型接口，也不代表真实模型效果、Qdrant 服务端、
存量 schema 升级或多机器部署已经通过验收。历史 `tests/` 仍有同步契约需要适配，
不能用新测试的结果替代旧测试的全量通过结论。

新增并发用例同时观察两张任务 RUNNING、第三张排队，再逐张核对退款、库存及
独立 thread 的 Checkpoint，并执行 SQLite 完整性检查。版本用例覆盖未知旧版本、
变更版本以及 API/worker 模型或业务配置漂移；期限用例覆盖真实定时提醒、到期交接、
重复请求不续期及过期批准与扫描竞争。`--browser` 需要 PATH 中的 Node 与 Chrome，
也可通过 `NODE_BINARY`、`CHROME_BINARY` 指定，浏览器产物同样在临时目录清理。

### 客服领取、图片和未读消息

用户点击转人工后，尚在执行的节点先安全交接，工单进入人工队列，再由客服领取。
领取在短事务中锁定工单，并通过包含原 `assignee_id` 条件的原子 UPDATE 修改归属；
不同客服抢同一张工单只有一个成功，其他请求返回 409 `TICKET_ALREADY_ASSIGNED`。
同一客服重复领取幂等；普通客服不能取消他人的领取或转派给别人，管理员可以显式转派。
锁定对象是这一张工单，不会把不同用户的所有会话串行化。

图片上传使用 [阿里云 OSS Python SDK V2](https://www.alibabacloud.com/help/en/oss/developer-reference/upload-files-using-oss-sdk-for-python-v2/)。
在 `backend/.env` 配置 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_REGION` 和 `OSS_BUCKET` 后重启。
当前这些配置留空；区域例如 `cn-hangzhou`，桶名需要填写实际已创建的私有桶。
`OSS_ENDPOINT` 可选；临时凭据另外填写 `OSS_SECURITY_TOKEN`。凭据只在后端使用。
SDK 显式使用这组凭据，不自动从机器其他云账号读取凭据；未配置返回 503，其他功能照常启动。

用户与已领取工单的客服可发送 PNG/JPEG/WebP 图片，默认最多 5 MB、4000 万像素。
后端检查实际图片内容、声明类型和工单权限，上传前后分别鉴权；OSS 网络请求期间不持有工单锁。
对象使用随机名称和私有 ACL；数据库仅保存元数据。详情返回附件信息，查看接口再次鉴权后
生成默认 300 秒的签名 URL，前端用 Element Plus `el-image` 展示和预览，过期可重新加载。
签名链接在有效期内持有者可访问，不能当作永久公开地址分享。
当前图片供人工核验，Agent 未接入视觉识别；提示词明确要求依赖图片证据时转人工。

上传前记录 PENDING 意图，消息与 READY 元数据在同一事务内绑定。
上传或提交失败时补偿删除对象；提交结果不明先核实，已绑定的图片不删除。
删除失败保留 CLEANUP_PENDING，worker 每分钟重试；超过一小时未完成的上传也会清理。
已成功绑定的附件不在垃圾清理范围内。单独部署 API 而没有 worker 时需启动 worker 才有定期补偿。

未读以 `(ticket_id, user_id)` 唯一游标分别记录，排除本人消息和系统提示；Agent 回复计入未读。
GET 详情不会自动清零。前端显示详情后，在页面可见时提交已显示的最大消息 ID；
数据库使用 `GREATEST` 原子更新，防止多标签页或乱序请求让游标倒退，之后到达的消息保留未读。
客服之间不共享已读状态。顶部角标和工单列表每 10 秒刷新；人工对话仍按原有 5 秒轮询。
新增表 `ticket_reads`、`ticket_attachments` 会在正常启动初始化数据库时建立。

本轮测试使用隔离 MySQL 库及模拟 OSS，覆盖实际上传 HTTP 接口、SDK 请求参数、图片校验、
权限、补偿重试、并发领取与已读游标。真实 OSS 的区域、桶权限和网络连通性需填入凭据后验证。
