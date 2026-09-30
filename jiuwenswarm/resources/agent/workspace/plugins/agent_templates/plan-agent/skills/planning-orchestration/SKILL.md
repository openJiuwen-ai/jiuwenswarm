---
name: planning-orchestration
description: 管理规划模块的输入自查、子进程调用、产物交付和 REPLAN 反馈传递；当用户需要跑规划模块、检查规划产物或基于反馈重规划时使用。
---

# 规划会话编排

## 目标

- 使用 `call_planning_skill` 工具把全局 planning Skill 当子进程黑盒调，产出模块二规划产物。
- 由当前 Agent 会话负责输入自查、4 个检查点人审和 REPLAN 反馈聚合；Skill 内部不持有跨调用状态。
- 最终产物（5 个 .json + 5 个 .md + status.json）可直接交给实验模块。

## 工作流

### 1. 收集输入

- 首跑：向用户要 `input_dir`（模块一产物目录，7 个输入文件的清单和必填字段由 input_validation rail 在每次模型调用前注入，不要凭记忆复述）。
- REPLAN：额外接收用户反馈，写成 `feedback_file` 传入；原始 `input_dir` 仍须同时提供。

缺输入时先向用户追问，不要猜测路径，也不要替 Skill 编造产物。

### 2. 调用 Skill

通过 `call_planning_skill`（操作类型 `run`）传入 `input_dir`（REPLAN 时加 `feedback_file`）。Skill 内部依次执行：输入加载 → 方法设计（含对抗评审与反思循环）→ 实验与数据规划 → 可行性门禁 → 执行配置 → 产物写出。

- `status.json` 的 `status` 为 `completed` 才算成功；`preflight_failed` / `failed` 时向用户展示 `errors`，不把部分产物当正式交付。
- Skill 自身不会发起人审；4 个检查点（方法设计 / 实验与数据计划 / 算力与可行性 / 执行配置）由当前会话在调用前后组织用户确认。

### 3. 展示与交付

向用户展示：

1. 方法创新点与组件设计摘要。
2. 实验方案、数据集订单与算力估算要点。
3. 可行性门禁结论（是否降级）。
4. `output_dir` 下完整产物清单。

### 4. REPLAN

收到用户反馈后，把反馈整理为 `feedback_file`，连同原 `input_dir` 再次调用 Skill。每轮只保留最近一次 `completed` 的产物作为下一轮基线；不要依赖 Skill 记住上一轮内容。

## 决策规则

- `input_dir` 缺文件或字段不全：先补输入，不调用 Skill。
- preflight 失败：按 `status.json` 的 `errors` 处理，通常是环境或包不完整，不要反复重试。
- 门禁未过且已降级：如实告知用户降级后的实验规模。
- 用户只要求解释当前产物：直接解释，不触发新一次调用。
- 用户明确提出修改：整理 `feedback_file`，触发新的子进程调用。

## 验收标准

- Skill 每次调用都是独立子进程，从显式输入重建全部上下文。
- 正式交付以 `status.json` 的 `status == "completed"` 为准。
- 4 个检查点全部由当前会话组织人审，Skill 内部不卡人。
- REPLAN 反馈只通过 `feedback_file` 进入 Skill，不通过会话历史泄漏。
