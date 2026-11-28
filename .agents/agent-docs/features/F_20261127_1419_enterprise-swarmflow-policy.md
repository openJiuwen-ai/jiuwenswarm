---
title: 企业Swarmflow策略接入（expert_package_template.swarmflow）
author: '@taoliankuan123'
id: F_20261127_1419_enterprise-swarmflow-policy
type: F
modules:
- jiuwenswarm
files:
- jiuwenswarm/enterprise/config/effective/db_queries.py
- jiuwenswarm/enterprise/config/effective/loader.py
- jiuwenswarm/enterprise/config/effective/schemas.py
- jiuwenswarm/enterprise/config/config_provider.py
- jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
- jiuwenswarm/agents/harness/team/team_manager.py
- jiuwenswarm/server/runtime/expert_team_batch.py
designs: []
design_note: 企业最小接入设计方案（外部文档 §4.2），仓内无独立设计文档；本文档为仓内行为契约的唯一维护位置。
---

# 企业Swarmflow策略接入（expert_package_template.swarmflow）

## 元信息

| 项 | 值 |
|---|---|
| 范围 | 企业配置链：db_queries/loader/schemas/config_provider；会话解析与装配：team_helpers、team_manager、expert_team_batch；前端门禁快照：sessionStore、App.tsx、InputArea.tsx、i18n zh/en（共 15 文件；frontmatter files 只登记后端核心） |
| 测试基线 | 后端 UT：test_session_swarmflow_config（含归一化/顺序/fail-closed/钳制方向）、test_team_helpers（swarmflow 域）、test_team_manager_registry（并发钳制向下收敛）、test_expert_team_batch、test_config 全绿；前端：input-area-permission-merge 套件除两条存量失败外全绿 |

## 实施要点

| 主题 | 实际做法 |
|---|---|
| 策略键契约 | `expert_package_template.swarmflow` JSON 列，键为 `allowed` / `default_enabled` / `max_workflows` / `agents_per_run` / `max_agents_total` / `swarmflow_budget`。布尔键 `_coerce_bool`（allowed 缺省 True、default_enabled 缺省 False）；数值键统一「无效 / 空 / null 即未下发」——键不进归一化结果（`_coerce_pos_int_or_none` + 删键），解析侧在键缺失时保留 config.yaml `swarmflow_concurrency` 部署底座，表单序列化出的 null 不会把部署已收紧的上限放大回引擎默认值 |
| 读取顺序契约 | `fetch_expert_package_swarmflow` 按 agent_template.template_ref 的 expert_package 引用列表顺序（入参 refs 顺序即优先级）取首个可规范化策略，与 `fetch_templates_by_slot` 的顺序契约一致，不依赖存储层返回顺序 |
| 配置链 | loader 在 `load_effective_enterprise_config` 读 expert_package 槽引用的策略挂到 `EffectiveEnterpriseConfig.swarmflow`；config_provider `_apply_enterprise_policy` 把策略经 `_merge_swarmflow_policy` 深拷贝合并进 `config.modes.team.jiuwen_team.swarmflow.*`（per-key update，保留本地未覆盖键） |
| fail-closed 行为 | 加载失败 ≠ 策略缺省：loader 异常时置 `swarmflow_load_failed=True`，`_apply_enterprise_policy` 在无策略且标记置位时合并 `{"allowed": False}` 并告警——瞬时 DB 故障不会把企业门禁静默放行；正常无策略（legacy 行）仍是缺省放行语义 |
| 解析优先级（开关） | request params > 会话 metadata 持久化 > 企业 `default_enabled`（显式下发才生效）> config.yaml `enable_swarmflow`；`allowed=False` 强制关闭生效值 |
| 预算语义 | 生效预算 = min(用户预算, 企业 `swarmflow_budget`)；解析结果同时携带用户意图（`swarmflow_user_enable` / `swarmflow_user_budget`，门禁/封顶前的原值）与生效值。两处落盘点（`_process_team_message_stream`、`expert_team_batch._prepare_owner`）只持久化用户意图 + 门禁/上限快照（`swarmflow_budget_cap` / `swarmflow_allowed`），企业策略每次解析重读——企业放开门禁或调高上限后旧会话自动跟随，不被上一轮强制值钉死 |
| 并发合并语义 | 三层上限（L1 max_workflows / L2 agents_per_run / L3 max_agents_total）只在企业策略显式提供时覆盖 spec 底座（config.yaml `swarmflow_concurrency` 经 config_loader 装配进 TeamAgentSpec），键间按「向下收敛到更紧上限」执行：`max_workflows = min(max_workflows, max_agents_total)`、agents_per_run 钳到 L3 内，企业 max_agents_total 是硬上限绝不被抬高；None 键视为未设值互相兜底后再钳制，钳制过程不抛 TypeError。上限键缺省时走引擎默认（16 / min(16, cpu-2) / 64） |
| 前端快照字段 | 会话恢复路径（App.tsx）读取持久化快照写回 store：`swarmflowAllowed`（false 时启用入口置灰 + `swarmflow.forbiddenHint`）、`swarmflowBudgetCap`（预算面板「企业上限」提示 + `setSwarmflowActive` 输入封顶）；`enable_swarmflow && allowed` 才恢复启用态。快照只在会话恢复时刷新，会话中途改策略由后端下一轮解析强制兜底。调度模式/启动即建队开关与 swarmflow 门禁解耦（只共享忙态 + 建队后冻结锁），禁止 swarmflow 不影响调度模式选择 |
| 验证 | 单测覆盖：归一化键契约（无效键剔除）、读取顺序（refs 优先于存储返回序）、fail-closed、用户意图/生效值分离、并发钳制方向（helpers 与 team_manager 两层，含仅下发 L3 的硬上限路径）、None 底座兜底；前端 input-area-permission-merge 套件回归。未做真实企业环境联调 |

## 遗留与边界

- TUI 及其他端尚未消费 `swarmflow_allowed` / `swarmflow_budget_cap` 快照（web 门禁先行）；后端强制关闭对所有端生效。
- 企业并发/预算为「每会话配额」，不做跨会话的全企业共享池；governor/ledger 跟团队实例同生命周期，重建/重启后计数归零。
- `swarmflow_load_failed` 仅由 loader 置位；手动写进 config.yaml 的 `jiuwen_team.swarmflow` 子字典开始被消费（新增能力），需在发版说明提示。
