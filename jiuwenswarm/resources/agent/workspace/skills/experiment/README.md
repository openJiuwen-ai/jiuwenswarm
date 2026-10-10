# 模块三：Experiment 实验执行模块

本目录是比赛项目的模块三源码交付包。它接收模块二给出的研究方法、实验计划、数据计划和资源限制，准备真实数据与实现，执行主实验、基线和消融实验，计算真实指标，完成分析并把可追溯结果和候选图表交给模块四。

模块三由两部分组成：

- `workspace/skills/experiment`：确定性底层代码，负责契约校验、状态机、数据准备、实现管理、真实实验执行、指标计算、分析、绘图和产物校验。
- `workspace/plugins/agent_templates/experiment-agent`：JiuwenSwarm Agent模板，包含根Agent、四个子Agent、Persona、工具桥接和专用工具白名单。

> 本交付包不是一个自带Python环境的独立程序。它需要合并到兼容版本的JiuwenSwarm源码仓库或工作区中运行。本目录不包含模型API密钥、Python虚拟环境、实验数据和历史运行结果。

## 1. 目录结构

```text
experiment/
└─ jiuwenswarm/
   └─ resources/agent/workspace/
      ├─ skills/experiment/
      │  ├─ SKILL.md                       # 模块三能力、约束和使用方法
      │  ├─ dependencies.yaml              # Python依赖声明
      │  ├─ references/
      │  │  ├─ experiment-schemas.md       # 模块三对外输入输出契约
      │  │  ├─ implementation-manifest.md  # 方法、指标和执行命令登记规范
      │  │  └─ internal-agent-contracts.md # 根Agent与四个子Agent内部交接契约
      │  ├─ scripts/                       # 确定性执行代码与CLI入口
      │  ├─ templates/                     # 实现清单模板
      │  └─ workflows/experiment.md        # 完整实验流程
      └─ plugins/agent_templates/
         ├─ marketplace.json               # Agent模板注册清单
         └─ experiment-agent/
            ├─ manifest.json               # 根Agent和四子Agent声明
            ├─ persona/                    # 根Agent规则
            ├─ subagents/                  # 四个子Agent
            ├─ skills/                     # 共享编排Skill
            └─ tools/                      # 工具桥接和工具白名单Rail
```

## 2. 四个子 Agent 的职责

根Agent `experiment-agent`维护上下文、查询状态、执行两阶段大模型审查，并严格串行委派：

1. `experiment-data-agent`
   - 检查数据来源、许可、大小和结构。
   - 准备数据并记录文件哈希和数据指纹。
   - 只能调用`experiment_prepare_data`。
2. `experiment-implementation-agent`
   - 查找、复用或生成主方法、基线和指标实现。
   - 生成代码只能写入本次`run_dir/implementations/<method>/`。
   - 只能调用`experiment_resolve_implementation`。
3. `experiment-execution-agent`
   - 在代码审查、冒烟测试和执行审查全部通过后运行冻结实验。
   - 保存配置、日志、返回码、预测文件和原始指标。
   - 只能调用`experiment_execute`。
4. `experiment-analysis-agent`
   - 审计真实指标，聚合多次运行，评价成功标准和研究假设。
   - 生成候选图表、原始绘图数据和模块四产物。
   - 只能调用`experiment_analyze`。

子Agent不能调用`bash`、`read_file`等通用工具，不能跨阶段操作，也不能创建孙Agent。大模型负责理解、消歧、受约束代码生成、审查和解释；真实科研计算始终由确定性Python代码完成。

## 3. 模块二需要提供什么

兼容适配层可以读取模块二的六个JSON文件：

```text
module2-output/
├─ method_design.json
├─ experiment_plan.json
├─ data_plan.json
├─ execution_config.json
├─ domain.json
└─ resource_constraints.json
```

模块二需要明确给出：

- 研究目标、核心方法和技术路线。
- 数据集名称、来源、许可、标签列、任务类型和切分策略。
- 基线、实验矩阵、随机种子和消融方案。
- 指标名称、比较方向和成功标准。
- 计算资源、时间限制和实验运行目录。

还需要提供`implementation-manifest.json`，用于登记数据目录、方法实现、运行命令、固定版本依赖、指标定义和审批状态。模板位于：

```text
jiuwenswarm/resources/agent/workspace/skills/experiment/templates/implementation-manifest.json
```

模块二的三列矩阵`[dataset, baseline, variable]`只有在数据含义明确、方法能由实现清单唯一确认时才会转换。模块三不会猜测方法、参数、指标或消融含义；无法安全转换时返回`REPLAN`和分类后的修改建议。

完整字段定义请阅读：

```text
jiuwenswarm/resources/agent/workspace/skills/experiment/references/experiment-schemas.md
```

## 4. 模块三交给模块四什么

完成后，模块四主要读取运行目录中的：

```text
outputs/experiment-module-output.json  # 结构化实验结论和全部引用关系
outputs/artifact-manifest.json         # 交付产物路径、大小和SHA-256
outputs/runtime-results.json           # 每次真实运行状态与原始指标来源
visualization/raw_metrics.csv           # 逐次运行原始绘图数据
visualization/aggregated_metrics.csv    # 均值、标准差和95% bootstrap区间
figures/candidates/                     # 候选SVG/PDF/PNG/TIFF图表
tables/                                 # 可用于正文或附录的候选表格
logs/                                   # 实验日志
```

候选图会保留逐次运行散点、均值和区间，并输出：

- 可编辑SVG；
- 矢量PDF；
- 300 dpi PNG预览；
- 600 dpi TIFF。

这些图是模块四选择、组合或重绘的高质量基础预览，不是已经完成全文排版的最终比赛图片。模块四可以调整布局和视觉样式，但不能修改源数字；如果需要改变统计或聚合方式，应由模块三重新计算。

## 5. 安装到 JiuwenSwarm

1. 将本交付包中的`jiuwenswarm`目录与目标JiuwenSwarm仓库根目录下的同名目录合并。
2. 不要删除或整体替换目标仓库原有的`jiuwenswarm`目录。
3. 如果目标仓库已有`agent_templates/marketplace.json`，应保留其中其他模板，只确认存在`experiment-agent`注册项，不要盲目覆盖新版清单。
4. 安装必需依赖：

```powershell
python -m pip install "pydantic>=2,<3" "matplotlib>=3.8,<4"
```

常用可选依赖：

```powershell
python -m pip install "psutil>=7.2.2,<8" "pandas>=2,<3" "scikit-learn>=1.4,<2"
```

如果规划需要XGBoost、PyTorch或Transformers，再按`dependencies.yaml`安装对应依赖。正式运行还需要在JiuwenSwarm自己的配置文件中设置支持工具调用的大模型；不要把API Key写进实验输入、实现代码、日志或交付产物。

重新启动JiuwenSwarm后，加载或安装`experiment-agent`模板。根Agent会读取共享的`experiment` Skill，并按四阶段顺序工作。

## 6. 确定性 CLI 用法

以下命令应从JiuwenSwarm仓库根目录执行：

```powershell
$ExperimentSkill = ".\jiuwenswarm\resources\agent\workspace\skills\experiment"
```

查看全部命令：

```powershell
python "$ExperimentSkill\scripts\main.py" --help
```

查看输入或输出JSON Schema：

```powershell
python "$ExperimentSkill\scripts\main.py" schema --kind input
python "$ExperimentSkill\scripts\main.py" schema --kind output
```

校验标准输入：

```powershell
python "$ExperimentSkill\scripts\main.py" validate-input --input .\request.json
```

把模块二分散文件适配为模块三标准输入：

```powershell
python "$ExperimentSkill\scripts\main.py" adapt-planning `
  --planning-dir .\module2-output `
  --workspace-root . `
  --manifest .\runs\run-001\implementation-manifest.json `
  --output .\runs\run-001\standard-input.json
```

运行无LLM的确定性流水线：

```powershell
python "$ExperimentSkill\scripts\main.py" run `
  --input .\runs\run-001\standard-input.json `
  --manifest .\runs\run-001\implementation-manifest.json
```

校验最终输出：

```powershell
python "$ExperimentSkill\scripts\main.py" validate-output `
  --input .\runs\run-001\outputs\experiment-module-output.json
```

CLI主要用于自动测试、故障排查、断点恢复和实验复现。正式Agent模式通过`experiment_agent_tools.py`调用同一套底层代码，不会在Persona里重新实现科研计算。

## 7. 状态机和断点恢复

主要状态顺序为：

```text
INITIALIZED
→ DATA_READY
→ CODE_REVIEW_REQUIRED
→ EXECUTION_REVIEW_REQUIRED
→ IMPLEMENTATION_READY
→ EXECUTION_READY
→ COMPLETED
```

也可能进入`REPLAN`或`FAILED`。状态保存在：

```text
<run_dir>/outputs/agent-state.json
```

使用相同`run_id`和`run_dir`可以查询并安全续跑。输入、实现代码、依赖、命令、数据或冒烟结果摘要发生变化时，已有审批自动失效并退回审查阶段，不能绕过`verified`、`ready`和`execution_approved`门禁。

## 8. 结果状态含义

- `PASS`：核心实验完成，证据、分析、图表数据、日志和复现信息齐全。
- `PARTIAL`：核心结果可信，但部分非核心实验缺失；失败记录仍完整保留。
- `REPLAN`：规划、数据、方法、指标或资源存在无法安全解决的问题，需要模块二修改。
- `FAILED`：没有形成可信且可复现的核心实验结果。

实验不支持原假设不代表程序执行失败。模块三会如实输出`NOT_SUPPORTED`或`INCONCLUSIVE`，禁止删除不利结果、伪造实验数值或把预期结果当成必须达到的目标。