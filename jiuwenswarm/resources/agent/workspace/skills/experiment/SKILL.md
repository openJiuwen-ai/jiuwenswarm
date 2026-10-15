---
name: experiment
description: 执行规划模块给出的科研实验方案，准备数据、运行主实验与消融实验、分析真实结果，并向写作模块交付可追溯且可复现的实验结论。用于 Planning 已产出 MethodDesign、ExperimentPlan、DataPlan 且编排层提供 Domain 后的第三阶段；不负责重新设计研究方法或撰写论文。
---

# Experiment Skill

## 目标

将规划模块的实验订单转化为真实、可复现的实验记录，并生成写作模块可以选择、组合或重绘的候选图表、绘图源数据和证据映射。

## 使用前提

1. 完整读取 [references/experiment-schemas.md](references/experiment-schemas.md)，按其中的 `ExperimentModuleInput` 接收输入并按 `ExperimentModuleOutput` 返回结果。
2. 执行或修改实验流程时读取 [workflows/experiment.md](workflows/experiment.md)。
3. 实现主方法、基线或指标时读取 [references/implementation-manifest.md](references/implementation-manifest.md)，按内部清单登记真实命令和指标文件。
4. 使用根Agent或任一阶段子Agent时完整读取 [references/internal-agent-contracts.md](references/internal-agent-contracts.md)，不得跳过状态或手改交接文件。
5. 当前接口仍是团队确认前的草案，`scripts/contracts.py`已与草案同步；使用 `python scripts/main.py validate-input --input <input.json>` 和 `validate-output` 做边界校验。

## 核心约束

- 不修改上游 `MethodDesign`、`ExperimentPlan` 或 `DataPlan`；不可执行时通过 `planning_feedback` 退回模块二。
- 模块二负责选择数据集、基线、指标和成功标准；模块三负责下载与预处理数据、定位或实现基线、实现指标计算并记录实际参数和版本。数据获取协议与研究领域和文件类型无关：有`source_url`时优先验证并下载；无地址时在Hugging Face与Zenodo等公开注册表做唯一精确匹配。精确数据不可用时，可根据任务、字段和指标语义返回有版本、许可和真实文件的兼容候选，但不得直接顶替执行；必须`REPLAN`到模块二同步重写数据集名称、字段映射、实验矩阵、基线和成功标准后才能重试。
- 使用编排层透传的`Domain`，结合研究方法、任务与结果选择分析方法和候选可视化；不得只按领域名称机械套用固定模板。
- 指标或实验矩阵存在会改变结论的歧义时，不得静默猜测，应返回 `REPLAN` 请求模块二明确评价意图或实验映射。
- `expected_results` 只是待检验预期，禁止把它当成必须达到的目标或据此篡改实验。
- 实验未支持假设不等于执行失败；只要流程有效且结果可复现，必须如实输出 `NOT_SUPPORTED` 或 `INCONCLUSIVE`。
- 分析方法和图表类型不设固定清单；实际方法、参数、假设、局限和领域适配理由必须写入结构化输出。
- 内置分析只处理标量test指标；领域需要混淆矩阵、残差、训练曲线、定性样例等专用分析时，通过`analysis_extensions`运行真实脚本并合并结构化产物。
- 必须同时交付至少一个`RAW`可视化数据资产和候选图/表；默认主交付目标为4幅、上限8幅互补候选图。数量是证据预算而非配额：真实证据不足时宁可少画并给出明确警告，也不得用同一批数据换图形重复凑数。
- 候选图根据论文结论、指标边界、重复运行分布和结果退化情况自适应选型，每幅图必须声明唯一证据角色、来源实验、实际展示指标和图注允许断言。每幅图导出可编辑SVG、矢量PDF、300 dpi PNG预览和600 dpi TIFF，并生成独立QA记录。它们是供模块四优先复用、必要时组合或重绘的高质量基础预览，不等同于已经完成全篇版式与叙事复核的最终比赛图片。
- 模块四可以选择、组合、排版或重绘候选，但不得改变源数字；需要改变统计分析或聚合方式时由模块三重新计算。
- 不得伪造数据、补写未运行实验、隐藏失败任务或把失败任务标记为成功。
- API Key、访问令牌和本地敏感路径不得进入输出与持久化产物。
- Agent模板只接受当前可信工作区内的输入、实现清单和运行目录；外部文件应先由用户复制进工作区。`verified=true`只能由确定性指标校验产生，`ready=true`只能由第一阶段审查放行后的真实冒烟测试产生。LLM只能提交审查决定，`execution_approved=true`只能由后端在双阶段审查与全部摘要一致时写入。

## 执行步骤

1. 标准输入直接校验；模块二分散输出使用`adapt-planning`组装。适配器只过滤模块二专属`result_dir`，三列矩阵只在实现清单能唯一证明方法和实验ID时转换，否则`REPLAN`。
2. Implementation Builder支持`inspect → reuse → build → test → register → request_approval`完整生命周期；为保证独立审查，实际安全展开为`inspect → reuse/build → static_check/register → request_approval(CODE) → 根Agent审查 → test → request_approval(EXECUTION)`。标准sklearn方法优先复用受控执行器；生成代码可由Agent直接提交结构化Proposal，但只能写入本次`run_dir/implementations/<method>/`，新增依赖只形成固定版本安装计划。
3. 把声明为`available`的非空真实数据放在`run_dir`内；模块会记录文件数、字节数和SHA-256树摘要。数据联网检索和下载默认开启，不再请求用户确认；DataAgent优先使用模块二的地址，缺地址时按名称检索公开注册表，只自动采用唯一精确匹配。不可用时产生兼容候选并触发有界规划闭环（默认最多3轮）；候选经模块二重写完整契约前绝不下载。所有检索记录写入`data/source-resolution/<dataset>.json`。仍无法唯一解析、许可不明或下载失败时返回`REPLAN`，提示把数据手动放入`execution_config.run_dir`下`manifest.datasets`指定的相对目录，再以相同`run_id`续跑。需要离线运行时可显式使用`--no-downloads`。
4. 代码审查通过后部署并验证本次运行环境。`CURRENT`模式验证当前解释器；`VENV`模式在`run_dir/environment/venv`创建隔离环境。依赖必须精确固定版本，只有显式启用时才安装。
5. 运行完整管线。不同子实验、方法和seed任务使用受控线程池并行，GPU任务另受独立并发上限约束；执行期间持续写`outputs/execution-monitor.json`和只追加的`outputs/execution-events.jsonl`，每次心跳重新校验配置、日志和数据指纹。
6. 分析真实指标并生成多层分析记录、候选图表和`outputs/writing-evidence.md`，供模块四充分撰写结果章节。
7. 检查`outputs/experiment-module-output.json`和`outputs/artifact-manifest.json`。`REPLAN`按`planning_feedback`修正规划或实现；`FAILED`查看`outputs/runtime-results.json`、实时事件和`logs/`；不得手工改科研数字。

Agent模式唯一顺序为`agent-init → agent-data → agent-implementation → CODE_REVIEW_REQUIRED → 根Agent隔离审查 → 受控冒烟 → EXECUTION_REVIEW_REQUIRED → 根Agent再次审查 → IMPLEMENTATION_READY → agent-execute → agent-analyze`。根Agent的两次LLM审查不能代替确定性门禁；后端把批准与代码、命令、依赖、清单及冒烟结果SHA-256绑定，任一内容变化都自动退回`CODE_REVIEW_REQUIRED`。同一`run_id`和`run_dir`可安全续跑。

对应的JiuwenSwarm模板位于`workspace/plugins/agent_templates/experiment-agent`：根Agent负责编排，四个直属子Agent分别调用上述四个阶段接口。模板只保存persona、编排skill和工具桥接；真实契约与科研计算仍以本目录代码为唯一来源。

常用命令：

```text
python scripts/main.py scaffold-manifest --input request.json
python scripts/main.py adapt-planning --planning-dir <module2-output> --workspace-root <trusted-workspace> --manifest <manifest.json> --output request.json
python scripts/main.py run --input request.json --manifest <run_dir>/implementation-manifest.json
python scripts/main.py validate-output --input <run_dir>/outputs/experiment-module-output.json
```

## 对外入口

模块编排层预留以下函数：

```python
def run_experiment_module(
    request: ExperimentModuleInput,
) -> ExperimentModuleOutput:
    ...
```

接口以 JSON 兼容对象传递；Python 实现使用 `scripts/contracts.py` 中的 Pydantic 模型进行边界校验。

## 完成条件

- `PASS`：核心实验完成，结果、证据、分析记录、可视化数据、候选方案、日志、复现信息和资源统计齐全，可交模块四。
- `PARTIAL`：核心证据与可追溯交付存在，但非核心实验缺失；由根 Agent 按工作流和 `ExperimentModuleOutput` 结果契约决定是否交模块四，不存在人工实现审批入口。
- `REPLAN`：计划不可执行或接口不一致，携带 `planning_feedback` 退回模块二。
- `FAILED`：没有形成任何可信、可复现的核心实验结果。
