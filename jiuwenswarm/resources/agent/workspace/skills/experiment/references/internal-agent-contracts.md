# 模块三内部 Agent 契约

本文件定义根 `experiment-agent` 与四个直属子 Agent 的实际交接。机器可读模型在 `scripts/agent_handoffs.py`，状态机实现在 `scripts/agent_coordinator.py`。

## 1. Agent 数量与职责

| Agent | 对外动作 | 只负责 | 成功状态 | 下一个消费者 |
|---|---|---|---|---|
| 根 `experiment-agent` | `adapt_planning` / `initialize` / `status` / `review_context` / 两阶段审查提交 | 输入边界、隔离审查、状态和串行编排 | `INITIALIZED` / `IMPLEMENTATION_READY` | DataAgent / ExecutionAgent |
| `experiment-data-agent` | `prepare_data` | 数据定位、地址解析、按名称检索、下载许可、完整性及预处理 | `DATA_READY` | ImplementationAgent |
| `experiment-implementation-agent` | `write_generated` / `request_approval` | 检查、复用/构建、静态检查、登记并形成代码审查上下文 | `CODE_REVIEW_REQUIRED` | 根Agent独立Reviewer |
| `experiment-execution-agent` | `execute` | 受控并行执行、实时校验、超时、有限重试、日志和原始指标 | `EXECUTION_READY` | AnalysisAgent |
| `experiment-analysis-agent` | `analyze` | 聚合、多层分析、判据、假设、按证据角色筛选的精简候选图、写作证据摘要和总输出 | `COMPLETED` | 模块四 |

子 Agent 只有一层，不得互相调用或创建孙 Agent。根 Agent 根据每次响应决定下一步。

## 2. 统一调用参数

根初始化接收：

```json
{
  "input_path": "模块二ExperimentModuleInput的JSON路径",
  "manifest_path": "本次implementation-manifest.json路径",
  "allow_downloads": true,
  "max_download_gb": 20
}
```

四个阶段子 Agent 接收同一个运行身份：

```json
{
  "run_dir": "本次运行目录",
  "run_id": "不可改变的运行ID"
}
```

DataAgent可额外接收`allow_downloads`和`max_download_gb`；`allow_downloads`默认`true`且无需再次请求用户确认，只有调用方主动要求离线时才传`false`。下载时模块二地址优先；地址缺失时，DataAgent仅采用公开仓库中的唯一精确名称匹配，并把解析审计写入`data/source-resolution/`。无法唯一解析或下载时，响应必须包含`manifest.datasets`对应的手动放置目录及相同`run_id`续跑提示。其他子Agent不能修改输入规划或实现清单。

## 3. 严格状态流转

```text
INITIALIZED
  → DATA_READY
  → CODE_REVIEW_REQUIRED
  → EXECUTION_REVIEW_REQUIRED
  → IMPLEMENTATION_READY
  → EXECUTION_READY
  → COMPLETED
```

`CODE_REVIEW_REQUIRED`和`EXECUTION_REVIEW_REQUIRED`都不是终态。第一阶段根Agent只能读取`outputs/code-review-context.json`中的可验证材料，并提交严格的`APPROVE_SMOKE/REJECT/REPLAN`；只有后端确定性门禁同时通过才运行最长60秒的受控冒烟。第二阶段根Agent读取新增的真实冒烟证据并提交`APPROVE_EXECUTION/REJECT/REPLAN`；只有代码摘要、冒烟摘要、指标、命令、数据范围和Reviewer身份全部一致时，后端才写正式批准。Implementation Agent不能审查自己。任何代码、命令、依赖、清单或冒烟结果变化都会清空两阶段审查并回到`CODE_REVIEW_REQUIRED`。

`outputs/agent-state.json`保存：`run_id`、当前状态、输入/清单/数据索引/环境部署/执行任务/原始运行结果/最终输出/交付清单的SHA-256摘要、产物路径、警告和完整状态转换记录。执行前后及运行心跳都会重新计算关键摘要；下一阶段会先复核摘要和任务—结果一一对应关系。任何人都不得手工编辑交接文件。

## 4. 五个响应结构

所有响应共同包含：

- `contract_version`：内部契约版本。
- `run_id`：本次运行身份。
- `run_dir`：后续子Agent必须原样使用的规范化运行目录。
- `stage`：持久化后的状态。
- `terminal`：是否已进入终态。
- `summary`：本阶段事实摘要。
- `warnings`：不改变科研数字的风险说明。

根`InitializeResponse`另外返回`next_agent`、`output_path`和完成态的`artifact_manifest_path`。

`DataAgentResponse`另外返回`consumer`、`prepared_dataset_count`和`datasets_index_path`。

`ImplementationAgentResponse`另外返回`consumer`、只读`inspection`及其路径、`execution_spec_count`、`execution_specs_path`、`implementation_review_path`、`review_phase`和`approval_required`。Implementation Agent先用`inspect`读取去敏的数据结构与完整方法/领域/资源/环境上下文，再执行后续受控动作。

根`RootReviewResponse`返回当前审查阶段、审查上下文路径/摘要、代码摘要、冒烟摘要和是否需要提交审查。根Agent必须为审查建立与Implementation Agent生成过程隔离的新上下文，不得接收生成过程的内部推理。

第一阶段决定必须严格为：

```json
{
  "decision": "APPROVE_SMOKE|REJECT|REPLAN",
  "reviewer": "experiment-agent",
  "review_model": "实际模型名称",
  "code_digest": "64位SHA-256",
  "security_findings": [],
  "scientific_findings": [],
  "dependency_findings": [],
  "required_changes": [],
  "reason": "审查理由"
}
```

第二阶段决定必须严格为：

```json
{
  "decision": "APPROVE_EXECUTION|REJECT|REPLAN",
  "reviewer": "experiment-agent",
  "review_model": "与第一阶段相同的实际模型名称",
  "code_digest": "与第一阶段一致的64位SHA-256",
  "smoke_result_digest": "真实冒烟结果64位SHA-256",
  "metric_contract_verified": true,
  "command_verified": true,
  "data_scope_verified": true,
  "risks": [],
  "reason": "审查理由"
}
```

冒烟仅选择每个计划方法的一个冻结任务、零重试且最长60秒。原始数据在4 MiB且不超过32个文件时可直接作为有限范围；更大的CSV/TSV/JSONL/NDJSON/TXT会复制最多512行的受限快照到`outputs/smoke-data/`，复杂的大型数据无法安全抽样时门禁拒绝。第二阶段上下文再次携带第一阶段冻结的完整代码材料，并分别列出正式数据范围与真实冒烟数据范围；`smoke-results.json`同时绑定每次冒烟的配置、日志和原始指标文件SHA-256，任一文件变化都会使批准失效。

`ExecutionAgentResponse`另外返回`consumer`、成功/失败运行数、`runtime_results_path`、`execution_monitor_path`、`execution_events_path`和最新`execution_progress`。根Agent在执行过程中可用`status`读取实时快照，不需要重启流程。

`AnalysisAgentResponse`另外返回`consumer`、`module_status`、`output_path`、完成态的`artifact_manifest_path`和`visualization_candidate_count`。

## 5. 给模块四的实际内容

AnalysisAgent完成后，主交接文件是`outputs/experiment-module-output.json`，其中包含科研发现、逐次运行、逐项指标、聚合与统计、判据/假设结论、证据映射、候选图表、复现信息和资源统计。

模块四同时使用：

- `visualization/raw_metrics.csv`：每次运行/seed的原始绘图数据；
- `visualization/aggregated_metrics.csv`：按实验、数据集、方法、指标和参数隔离后的聚合数据；
- `visualization_candidates`：候选图表的用途、数据引用、实验引用、图像/规范路径；
- `outputs/writing-evidence.md`：按证据覆盖、真实发现、聚合结果、分析限制和候选用途整理的结果写作材料；
- `outputs/execution-monitor.json`与`outputs/execution-events.jsonl`：并行执行状态、心跳、实时校验、重试和失败证据；
- `environment/deployment.json`与`environment/requirements.lock`：实际解释器、依赖部署和版本校验记录；
- `table_artifacts`与`figure_artifacts`：已生成预览及可编辑规格。
- `outputs/artifact-manifest.json`：模块四全部伴随文件的路径、字节数和SHA-256；读取前必须校验，防止CSV、预览或可编辑规格被误改。

模块四可选图、合并子图或基于源数据重绘，但不能修改源指标或偷偷改变聚合方式。
