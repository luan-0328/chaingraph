# RAG 评测与改进说明

本轮完成：规则与实现边界校对、140 条人工编写问题、证据标注、固定划分、真实语义
Embedding 对比、开发集参数搜索、逐题失败报告、离线回归门禁和回答日志导出。
未自动切换生产检索参数，也未更新原业务库知识、向量索引或调用退款动作。

## 数据怎么建

数据见 `evals/rag_suite.json`：70 个意图组、每组两种问法，共 140 条。
49 组常规问题、14 组未知问题、7 组多条件场景。每条包含：

- `group_id` / `split`：同意图的改写必须同组，不能跨集；98 条开发、42 条回归留出。
- `evidence_groups`：每组必须命中一份证据；`any_of` 表示可替代来源，多组必须同时找到。
- `reference_answer`：人工编写的参考答复；`expected_behavior` 和 `required_tools` 是回答/Agent 评测目标。
- `must_not_claim`：待人工复核的危险短语，不能把字符串匹配当作语义判定。

标签按文件和原文定位，不绑定容易随切分变化的 chunk 编号。规则改动导致原文不存在时，
校验直接失败，不静默删题。对等价来源的标注仍可能不穷尽；失败样本需人工复核。

当前集合由代理根据演示规则编写，尚未由业务人员独立审定。部分题源已在旧 63 题基线中
公开，因此 `test` 不是严格盲测，也不代表真实用户分布。留出集特意含更难的域内未知问题，
开发集未知问题偏容易，二者难度分布不一致，不能只用开发集指标宣称泛化效果。
本轮见到留出结果后没有继续调参。下一版本应补充开发集中的难负例，并另建未见过的
独立验收集；已用于诊断的当前留出题保留为回归题，不再作为新的盲测数据。

## 怎么运行

在 backend 目录：

```bash
# 免费离线评分、标签校验、48组参数的开发集搜索
uv run python scripts/run_rag_evaluation.py
# 同时比较已配置语义Embedding；首次会产生向量接口调用，后续使用缓存
uv run python scripts/run_rag_evaluation.py --providers local configured
# 固定旧参数、本地哈希向量的防退化门禁，不调参、不调用外部模型
uv run python scripts/check_rag_regression.py
# 评分、分组、客户端批次和日志导出的单元回归
uv run pytest tests_rag -q
```

多 Provider 对照报告在 `docs/rag-evaluation.json` / `.md`。默认命令仅包含 local，
如需保留语义对照报告，单独设置 `--output data/local-evaluation.json`。
缓存位于忽略提交的 `data/rag-eval/`，以语料正文、问题顺序、模型名称及服务地址指纹隔离。
参考答案或评分标签变化不需要重算向量。若服务商在同模型名下更换模型版本，需删除对应
本地缓存后重跑；缓存不能自动检测供应商内部版本变化。
不向报告写入 API Key 或完整服务地址。没有配置语义模型时 `configured` 直接报错，禁止伪装回退结果。

本轮语义模型为已配置的 `qwen3.7-text-embedding`（1024维），哈希向量512维。
全部向量先归一化后做精确余弦排序。调参只在 dev 搜索 Top K 为 2/4/6、16档阈值，
以 `(完整证据率 + 未知拒检率) / 2` 选择方案；同分优先少误拒和更高召回。
候选固定后才评估 test，不根据 test 改选。该规则是当前实验目标，不是经过业务批准的发布门槛。

## 实测与限制

同一份校对后的 7 篇文档、59 块、140题，不与旧63题基线直接计算提升百分比。
以下为42条留出问题，其中34条有依据、8条为域内未知；小样本波动较大。

| 方案 | Recall@K | 完整证据率 | 未知拒检率 | 有据误拒率 |
|---|---|---|---|---|
| 哈希向量，K=4，阈值0.05 | 52.9% | 50.0% | 0% | 0% |
| 语义向量，K=4，阈值0.05 | 91.2% | 88.2% | 0% | 0% |
| dev选出的语义候选，K=6，阈值0.55 | 97.1% | 94.1% | 12.5%（1/8） | 0% |

语义Embedding显著改善这批问题的证据召回。单纯升阈值无法可靠识别“与退款相关但
知识库没有答案”的问题；同候选在dev未知拒检95%，test只有12.5%，说明开发集指标
不能直接作为上线依据。检索有返回也不等于模型必然编造，需继续评估最终答复是否有足够依据。

这是离线检索实验，未测 Agent 的查询改写、Qdrant ANN、发布/下线过滤、工具调用或真实答案。
生产代码新增可配置 `RAG_TOP_K` / `RAG_MIN_SCORE`，默认仍为 4 / 0.05，避免把实验候选
误当通用参数；代码中原先写死的节点 Top K 已改为读取配置。配置写入执行版本指纹，
升级时 API/worker 需保持一致。没有修改真实 `.env` 或自动重建业务索引。
知识文档改动需要管理员复核发布后才能作用于存量业务库，不能把本次磁盘评测等同线上已生效。

## 自动化回答评测

先生成待运行任务：

```bash
uv run python scripts/export_rag_answers.py
```

输出 `data/rag-answer-tasks.jsonl`，含问题、候选检索上下文、参考答案、行为和工具要求。
没有真实回答时不会捏造 `response`，不能拿这个文件直接宣称回答评测通过。

在独立测试环境运行 Agent 或答复模型后，逐题保存 JSONL，字段如下：

```json
{"case_id":"policy-006-1","response":"实际生成的答复","retrieved_contexts":["该次模型实际收到的片段"],"behavior":"grounded_answer","tool_calls":[]}
```

`behavior` / `tool_calls` 应来自实际执行日志；不提供就记为未测。该例仅说明格式，
完整评分必须覆盖报告中42条题的真实ID。不能只导出答对的题。

```bash
uv run python scripts/export_rag_answers.py --answers data/actual-answers.jsonl --output data/ragas-input.jsonl
```

脚本检查题目完整性、重复ID、实际上下文和非空答复，并输出行为/工具的确定性检查。
禁用短语只做人工复核告警，可能因否定或引用出现误报。导出的 `user_input`、`response`、
`retrieved_contexts`、`reference` 可传入 Ragas 的 EvaluationDataset。
本轮没有生成真实回答、运行 Ragas/模型裁判，或把待运行任务冒充质量结果。

可用工具：

- Ragas：Context Recall/Precision、Faithfulness、回答相关性等，部分指标需要裁判模型或Embedding。
  官方说明：https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
- Promptfoo：测试配置、确定性断言、模型裁判和CI报告，适合比较Prompt或模型版本。
  官方说明：https://www.promptfoo.dev/docs/configuration/expected-outputs/

模型裁判也会误判。固定裁判模型、Prompt和评分规则，保留理由，抽查高风险退款题；
若生成与评分使用同一个模型，要单独说明偏差风险。自动生成题只作为候选，人工确认标签
后再入正式集；不应让同一个模型包办出题、答案和评分后就宣布业务正确。

## CI与验证

新增 `.github/workflows/rag-evaluation.yml`，相关PR或手动触发后运行离线单元测试和
固定参数回归，不读取密钥、不访问业务数据库或外部模型。工作流已写入，未推送或远程执行。
基线 `evals/local-regression.json` 只防止已有行为退化，不证明低质量哈希方案可上线。
语料/标签指纹变化时门禁失败，需审阅对照报告后显式更新，CI不会自动重置基线。

本轮本机：13项评测单元测试通过；6项相关隔离业务回归通过（82项未选中），
覆盖自动退款、审批恢复、工单并发、SSE及版本不兼容拒绝。中文注释检查通过。
真实Embedding成功处理全部语料与问题；发现并修复兼容服务单批最多20条的限制，
客户端每批16条且请求超时30秒。第三方langsmith弃用警告仍存在。
