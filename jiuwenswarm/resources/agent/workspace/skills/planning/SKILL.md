---
name: planning
description: "基于 JiuwenSwarm 的科研论文「规划模块」（模块二）Swarm Skill：把研究问题转换为论文级的方法设计与实验蓝图。3 角色混型编排（C+A）：方法设计→对抗评审→反思循环→实验与数据规划。使用当拿到研究问题、假设、gap 报告与关键论文，需要产出方法创新点、组件设计、实验方案、数据集订单与算力估算时。不要用于论文全文撰写、实验代码写跑、文献综述展开——那些由后续模块负责。"
---

# Planning（科研论文规划模块）

科研论文自动生成流程的「模块二：规划」独立 Swarm Skill。它是论文创新点的源头：

- **上游输入**：research_question / hypotheses / gap_report / key_papers（来自模块一构思）。
- **下游产物**：`method_design` / `experiment_plan` / `data_plan`（供模块三实验直接执行、模块四写作直接引用）。

> 说明：本次 CCF BDCI 2026 流程分构思→规划→实验→写作四模块。规划模块先独立成 Skill，
> **四个模块各自完成后**，再写一个 `research-paper-generator` 调度 Skill 编排它们。

## 混型编排模式：C+A

先串行推进（C：方法设计 → 实验规划），其中方法设计阶段内置**对抗式反思循环**（A）：
Method Designer 出初稿 → Method Critic 对抗评审 → 未通过则带 feedback 定向重写 → 通过后进入实验规划。

## Workflow

0. **依赖检查** —— 阅读 [dependencies.yaml](dependencies.yaml) 核对依赖。缺失 `required: true` 项会严重影响运行；`required: false` 项自动降级（如外部检索降级为纯推理）。是否继续由用户决定。
1. **方法初稿** —— method-designer：生成 `method_design`（研究目标 / 框架组件+数据流 / 创新点 / 假设覆盖 / 局限）。
2. **对抗评审** —— method-critic：逐创新点 novelty 三要素判定、总评、风险、缺口、缺失字段，给出 `verdict`。
   未通过且 ≤ `MAX_METHOD_ROUNDS`：定向 feedback 回传 designer 重写后重评。超轮未过 → 完整记录并交用户裁决。
3. **实验规划** —— experiment-planner：生成 `experiment_plan`（主实验/消融/基线/指标/算力）与 `data_plan`（数据订单/总成本）。
   算力超预算 → 按三档降级（见 [references/planning-schemas.md](references/planning-schemas.md) §6）重排。
4. **产物汇总** —— Leader 整理三件套 + 未解决项备注。

## Agents

| id | 职责 | 何时派出 | 输入 | 关键依赖 | 定义文件 |
|---|---|---|---|---|---|
| method-designer | 方法架构师：出初稿并按评审定向重写 | 每轮方法设计 | research_question / hypotheses / gap_report / key_papers / resource_constraints / domain | 外部检索（novelty 查重，可降级） | [references/method-designer.md](references/method-designer.md) |
| method-critic | 方法评审：对抗式找缺陷、给通过判定 | 每轮初稿后 | MethodDesign 初稿 / gap_report / key_papers | 外部检索（novelty 对照，可降级） | [references/method-critic.md](references/method-critic.md) |
| experiment-planner | 实验规划：实验+数据+算力 | 方法定稿后 | 定稿 MethodDesign / MethodReview / ResourceConstraints / Hypotheses / KeyPapers | 外部检索（数据集/基线，可降级） | [references/experiment-planner.md](references/experiment-planner.md) |

> 派出每个 teammate 前，读取对应 agent 定义文件的 `## Inline Persona for Teammate` 段并原样粘贴进派发 prompt——teammate 访问不到本文件。

## Files

| 文件 | 内容 | 何时读 |
|---|---|---|
| [workflows/planning.md](workflows/planning.md) | 规划模块完整编排：mermaid 图 + 分步协议 + 质量门禁 + 最终产物格式 | 派发前必读——完整剧本 |
| [references/planning-schemas.md](references/planning-schemas.md) | 强格式接口契约：MethodDesign/MethodReview/ExperimentPlan/DataPlan 逐字段中文注释 + 评审/算力门禁 | Agent 输出时——契约束缚 |
| [references/\*.md](references/) | 各 agent 的身份、成功标准、边界、Output Schema、Inline Persona | 派发每个 teammate 前——抽取 Inline Persona |
| [scripts/main.py](scripts/main.py) | CLI 编排入口（3 阶段串联） | 主流程——`python scripts/main.py --input-dir <dir> --output-dir <dir>` |
| [scripts/iterate_method_design.py](scripts/iterate_method_design.py) | 第 1 阶段：方法设计 + 反思循环 + 假设覆盖兜底 | Phase A 启动时 |
| [scripts/plan_experiment_with_data.py](scripts/plan_experiment_with_data.py) | 第 2 阶段：实验规划 + 抽数据订单 | Phase B 启动时 |
| [scripts/check_feasibility_and_downgrade.py](scripts/check_feasibility_and_downgrade.py) | 第 3 阶段：门禁校验 + 算力降级 | Phase C 启动时 |
| [scripts/_subagent.py](scripts/_subagent.py) | sub-agent 拉起（persona 抽取 + `facade.agent()` 包装 + JSON 解析） | 3 个阶段派发 sub-agent 时 |
| [references/swarmflow-facade.md](references/swarmflow-facade.md) | SwarmFlow `facade` 全 API 文档（`agent`/`human`/`phase`/`log`/`pipeline`/`parallel` 等） | 写新 workflow 或调 LLM 时 |
| [scripts/load_inputs.py](scripts/load_inputs.py) | 解析模块一 8 个输入（含完整 `references`，兼容旧缓存缺省） | 启动时 |
| [scripts/write_outputs.py](scripts/write_outputs.py) | 写 4 产物（.json + .md 双格式） | 主流程收尾 |
| [scripts/validate_plan.py](scripts/validate_plan.py) | 输出契约束校验（CLI 工具） | Agent 输出后校验 |
| [scripts/estimate_compute.py](scripts/estimate_compute.py) | 算力估算 + 三档判定（CLI 工具） | 算力预算比对时 |
| [dependencies.yaml](dependencies.yaml) | 外部 skill 与工具依赖 | 启动时——核对依赖、报告缺失项、用户拍板 go/no-go |
