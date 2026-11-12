---
id: S_01_team-goal-config
title: Swarm 团队 Goal 配置契约
type: S
author: alan_cheng
author_email: chengshuo5@huawei.com
updated: 2026-11-11
modules:
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/common/config_panel
  - jiuwenswarm/server/control
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/channels/web/frontend/src/features/settings
files:
  - jiuwenswarm/common/team_goal_config.py
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/config_panel/config_set_handlers.py
  - jiuwenswarm/agents/harness/team/config_loader.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
---

# Swarm 团队 Goal 配置契约

本规约负责 Swarm 的设置、配置装配与 Web 终态适配。目标持久化、评估、次数统计及恢复由 agent-core 的 `openjiuwen.agent_teams` 负责，不在 Swarm 另建目标管理器。

设计依据：[D-20261111-1722-team-goal-config](../../../../../designs/active/D-20261111-1722-team-goal-config/design.md)。实施与证据：[F_20261111_1755_team-leader-goal](../../../../../features/F_20261111_1755_team-leader-goal.md)。

## 配置与设置接口

`modes.team.<模板>.leader.goal` 和 `member_goal` 支持对象或 null，缺省为关闭。对象仅包含 `strategy`（默认 hybrid，可选 agent_report/transcript/hybrid）与 `max_attempts`（默认 10，正整数或显式 null）。bool、零、负数、小数、字符串次数和未知字段均拒绝；不接入 token_budget。

前端布尔设置 `team_leader_goal_enabled` 映射默认模板的 `leader.goal`，不改变 member_goal。仅在 dispatch_mode 为 scheduled 且策略为对象时读取 true，空对象亦为启用。开启时保留已存在的有效参数，否则创建 hybrid/10；关闭时写 null，不保留隐藏停用策略。

同次设置提交按最终调度模式和目标开关统一校验，两字段由同一次锁内原子替换写入，不依赖键顺序。最终为 autonomous 却显式开启目标时返回配置错误，先于这些字段的持久化。关闭调度模式必须同时清空 leader.goal；再次开启调度模式不会恢复目标策略。

## 生效与快照不变量

- I-1：配置 loader 及最终会话模式覆盖后，非 scheduled 的 leader.goal 必须为 None。loader 不回写原始模板。
- I-2：leader 开关不得启用 member_goal；成员策略独立由 YAML/模板声明，适用模式交由 core 判断。
- I-3：已绑定团队保持模板快照；目标设置保存仅更新默认配置及 UI，不重建正在运行的团队或重置目标次数。
- I-4：启用策略需要依赖实际公开 LeaderSpec.goal 和 TeamAgentSpec.member_goal；旧 schema 必须报错，不能静默丢弃。CLI leader 启用目标必须报错。
- I-5：内部 Summary Team 独立构造配置，不继承用户模板的目标开关。

## 阻塞事件适配

core resident stream 中 `team.goal.blocked` 由 Swarm 转换为带 `TEAM_GOAL_BLOCKED` 的 `chat.error`，并发送 `chat.processing_status`，其中 is_processing=false、is_complete=true、reason=team.goal.blocked。使用 core 的 reason；缺失时提供中英文兜底。该状态结束当前请求处理，不把未完成任务标记完成，不生成 team.completed，resident stream 保留以接受后续输入。

目标身份与轮数恢复沿用 core 和团队快照机制；本设置不新增暂停、恢复接口或目标详情前端。

## 回归入口

- `tests/unit_tests/common/test_team_goal_config.py`：配置持久化、读取、校验、联动、原子写入与 reload scope。
- `tests/unit_tests/agentserver/test_team_config_loader.py`、`test_team_dispatch_mode.py`：真实 schema 装配、模板快照和最终会话模式门控。
- `tests/unit_tests/agentserver/test_team_helpers.py`：真实事件解析及 resident stream 的阻塞终态适配。
- `jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs`：声明式设置契约及父子联动。
