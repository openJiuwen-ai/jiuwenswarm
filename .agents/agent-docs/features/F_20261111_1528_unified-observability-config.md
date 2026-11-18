---
id: F_20261111_1528_unified-observability-config
title: Team 与单 Agent 可观测性配置统一为 observability 段
type: F
author: alan_cheng
author_email: chengshuo5@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/agents/harness
  - jiuwenswarm/resources
  - deploy/yuanrong
  - scripts
files:
  - jiuwenswarm/common/observability_config.py
  - jiuwenswarm/common/config.py
  - jiuwenswarm/agents/harness/agent_observability.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/resources/config.yaml
  - deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml
  - scripts/reset_trajectory_data.py
  - tests/unit_tests/common/test_observability_config.py
  - tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py
designs: []
design_note: 用户在会话中确认的配置收敛改动，无独立设计编号。
---

# Team 与单 Agent 可观测性配置统一

## 背景

原先 `team_observability` 和 `agent_observability` 是两段平行的顶层配置，字段完全相同，都经 `build_observability_config` 生成同一个 `ObservabilityConfig`。OTel 一个进程只有一个全局 TracerProvider，两边同时开启时先初始化的一方生效，另一方的 exporter / endpoint / service_name 被静默忽略。分成两段的配置在同一进程里本来就无法各自生效，真正按运行时区分的只有开关。

## 实施与决策

- **新配置形态**：顶层 `observability` 段，`team.enabled` 和 `agent.enabled` 是两个开关，其余字段是共享的导出配置。默认值沿用旧模板：Team 开启、Agent 关闭、exporter 为 `file`。`service_name` 统一为 `jiuwenswarm`，单 Agent 不再默认使用 `jiuwenswarm-agent`。
- **升级迁移**：`migrate_legacy_observability_config` 挂在 `migrate_config_from_template` 里，在 `_deep_merge` 之前执行（与 KVC、plan/fast memory 迁移同一位置），会删除旧的两段。新段里已有的值优先，旧值只补缺失的键。两段旧配置的共享字段取值冲突时，按以下顺序决定：
  1. 两边相同，直接沿用；
  2. 只有一边改过旧模板默认值，取改过的那边（避免另一段留下的默认值盖掉用户的修改）；
  3. 两边都改过且互相冲突，取 enabled 的那段；两段都 enabled 或都没 enabled 时取 Team，并记 WARNING。

  两边都保留各自默认值的字段（只有 `service_name`）不写入，由新模板补齐。
- **运行时兜底**：AgentOS 下会跳过模板迁移，因此运行时 `resolve_observability_section` 会在内存副本上执行同一套迁移，旧配置照样生效，每个进程只打一次弃用告警，不改动调用方传入的 config。
- **同步改动**：Team / Agent 的 sync 函数、`scripts/reset_trajectory_data.py`、yuanrong 部署模板、`config.yaml` 中相关注释、`docs/{zh,en}` 的调试追踪 / TTSE / 配置说明，以及 `deploy/observability/README.md`。

## 验证

- worktree 独立 `.venv`：执行 `uv sync --frozen --no-install-package openjiuwen-computeruse`。computeruse 的 Swift 原生代码在本机工具链下编译失败，与本次改动无关。
- 新增 `tests/unit_tests/common/test_observability_config.py`，覆盖：无旧段时不做任何改动；全默认值合并；单边修改优先；冲突时取 enabled 一方；冲突时回落到 Team；只有一段旧配置；新段优先；非 mapping 的旧段；运行时解析不改入参且只告警一次；基于真实模板的升级迁移，以及迁移结果的幂等性。`test_trajectory_runtime_wiring.py` 新增两个用例：未迁移的旧配置仍能驱动 Agent；Team 与 Agent 读到同一个导出块。
- `pytest tests/unit_tests/common/test_observability_config.py tests/unit_tests/agentserver/{test_trajectory_runtime_wiring,test_debug_trace,test_ttse_plugin_runtime,test_observability_config_mapping}.py tests/unit_tests/test_config_template_key_coverage.py tests/unit_tests/test_config_open_ended_maps.py`：143 passed。
- `pytest tests/unit_tests/test_config.py tests/unit_tests/common/test_agentos_config_migration.py tests/unit_tests/agentserver/test_team_helpers.py tests/unit_tests/observability`：626 passed。

## 遗留与交接

- 分布式 leader / teammate 配置模板仍然没有 `observability` 段，靠模板迁移补齐；需要观测的部署要在每个进程的配置里写好 endpoint。
- 没有在真实 OTLP 后端（如 Jaeger）上做端到端验证。
