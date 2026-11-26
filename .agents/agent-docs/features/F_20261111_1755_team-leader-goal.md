---
id: F_20261111_1755_team-leader-goal
title: 调度模式领导目标驱动设置实施与验证
type: F
author: alan_cheng
author_email: chengshuo5@huawei.com
created: 2026-11-11
updated: 2026-11-11
modules:
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/common
  - jiuwenswarm/common/config_panel
  - jiuwenswarm/server/control
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/channels/web/frontend/src/features/settings
files:
  - jiuwenswarm/common/team_goal_config.py
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/config_panel/config_set_handlers.py
  - jiuwenswarm/server/control/config_service.py
  - jiuwenswarm/agents/harness/team/config_loader.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/experimental/definition.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts
  - jiuwenswarm/resources/config.yaml
  - pyproject.toml
  - uv.lock
designs:
  - D-20261111-1722-team-goal-config
---

# 调度模式领导目标驱动设置实施与验证

## 依据与交付范围

用户明确批准按 [D-20261111-1722-team-goal-config](../designs/active/D-20261111-1722-team-goal-config/design.md) 实施、测试并创建 Issue / PR。当前契约维护在 [S_01_team-goal-config](../specs/jiuwenswarm/agents/harness/team/S_01_team-goal-config.md)。本记录是该特性的首次实施记录，不替代其他开发记录。

基线为 `c1d9e6ad8`；候选位于独立分支 `codex/team-leader-goal`，与原工作区尚未提交的 computer-use Swift 修复隔离。实现配置层 leader/member 策略、调度模式下 leader 布尔子开关、模式门控及阻塞终态。未增加 token 预算、成员前端控件、目标详情或暂停恢复接口。

## 实际实现与决策

- 普通布尔配置注册 Settings 契约，沿用实验特性调度模式 subItems、SettingItemRenderer 及现有父子联动，没有自定义组件或布局样式。
- 统一纯函数负责策略校验与模式/开关决策，Front 读取及 Runtime 配置面板共享读取规则，避免空对象被误判关闭。
- 调度模式与 leader.goal 在 config.update_config 的一次锁内原子写入；启用自主模式目标在写入前拒绝。Goal 本身仅触发 web_ui 刷新，避免销毁活动团队。模式切换继续沿用原有 reload 行为。
- loader 按模板快照构造真实 TeamAgentSpec，TeamManager 在会话模式覆盖后再次清除 autonomous 的 leader.goal。原始 YAML、member_goal 和已有团队策略不会被这个运行门控改写。
- core 阻塞事件并不关闭 resident stream。Swarm 显示原因并结束本轮 processing 状态，保留后续用户输入入口，避免前端等待不结束；不伪造任务完成。
- 固定 core 从 `f3f8f576164cc405c046e43079c8ad00ee17f0c4` 对齐到实际包含目标 schema 的 `4836fc2815b897bf022e36d5df22481fb446047d`，覆盖直接依赖、extras override 和 uv source，测试使用固定安装包而非本地 editable core。
- 新 core 要求 OpenTelemetry >=1.45.1，与现有 gateway <1.42 声明冲突。因此宿主统一 SDK/API/exporter 为 >=1.45.1,<2，uv override 对齐 gateway，未升级 gateway 提交。锁文件中的相关安全下限升级由该 core 依赖引入。gateway 未采样 traceparent 测试改为解码校验 sampled 位及 trace/span 身份，兼容新 SDK 的 random-trace-id 标志位，未放宽未采样要求。

与批准范围无功能偏差。统一策略辅助函数放在 common，供 loader、Front 和 Runtime 复用；这是对设计中目标函数位置的实现细化。

## 环境与执行证据

环境为 macOS arm64、Python 3.13.5、独立 uv venv。`uv sync` 使用锁定 core 包；computer-use 保持远程原 pin，安装时通过 `COMPUTER_USE_BACKEND_DIR=/Users/alan/Developer/jiuwen/computer-use/build/lib/computer_use/_resources` 复用前序已修复的本机原生后端，避免混入本次 PR。该安装结果不证明原 pin 在 Swift 6.3.3 下可从零编译。

以下从仓库根执行 Python 前均激活 `.venv` 并导出 `PYTHONPATH=.:$PYTHONPATH`：

```bash
python -m pytest \
  tests/unit_tests/common/test_team_goal_config.py \
  tests/unit_tests/common/test_team_member_subtasks_config.py \
  tests/unit_tests/common/test_team_member_messaging_config.py \
  tests/unit_tests/agentserver/test_team_config_loader.py \
  tests/unit_tests/agentserver/test_team_dispatch_mode.py \
  tests/unit_tests/agentserver/test_team_helpers.py \
  tests/unit_tests/gateway/test_gateway_telemetry.py \
  tests/unit_tests/observability/test_core_processor_integration.py \
  tests/unit_tests/agentserver/test_observability_config_mapping.py \
  -o addopts= -o asyncio_mode=auto --no-cov -q
```

结果 **483 passed, 2 skipped**。补充测试类型注解及明确模拟 stream 参数后，Goal 相关 6 项定向复验通过；`uv lock --check` 与 `git diff --check` 通过。覆盖配置真实文件写入后独立读取、Front/Runtime 读取一致、开关字段顺序、无效配置不半写、手工参数保留、旧 core 拒绝、快照优先、最终会话覆盖以及真实 parser 接收阻塞事件时的 request 终态与 resident stream 保留。

额外在固定安装的 core 上执行其同提交 `test_team_goal.py` 和 `harness/test_goal_rounds.py`：**38 passed**。测试仅将 core tests 注册为 tests package，未把 core 源码加入 PYTHONPATH。覆盖目标绑定、续跑、次数阻塞、追加输入恢复和 NativeHarness 链路；模型输出为确定性模拟，不代表真实外部模型端到端验收。

前端目录执行：

```bash
node --test tests/settingsRefactor.test.mjs tests/settingsChannelAdapters.test.mjs
npm run build
```

结果 **47 passed**，TypeScript/Vite 构建成功。完整 `npm run test:settings-panel` 为 **66 passed, 1 failed**；失败位于 `tests/modelSettings.test.mjs:721` 的旧静态正则断言（models.references / MODEL_SELECTION_REFERENCED）。在原基线工作区同命令复现相同失败，未将其计入本次通过数量。

## 用户可见行为与验证范围

使用真实 SettingItemRenderer、设置 Provider、实验模块定义和中英文资源进行浏览器交互验证，SettingsRequest 使用内存模拟后端：父关闭时 goal 禁用；父开启可启用 goal；关闭父清除 goal；重新开启父不恢复 goal。验证中文桌面、英文 640 像素窄屏，浅色主题无重叠。截图：[中文](../../../docs/assets/images/team-leader-goal/zh-enabled.png)、[英文窄屏](../../../docs/assets/images/team-leader-goal/en-narrow.png)。临时预览入口已删除，未写真实用户配置。该证据覆盖真实组件及联动，不代表完整应用经网络到实际后端的 E2E。

已沿设置字段、配置持久化、loader、会话模式覆盖、core 事件生产与 Web 消费链检视差异。同步中英文 AgentTeam 文档、资源模板、当前 spec 和设计阶段管理信息。core 内部契约仍由 core 维护，未复制到 Swarm。

## 限制与后续验证

- 未执行真实外部 LLM 的完整 Swarm 端到端会话、实际进程重启恢复、Windows/Linux 验证或性能测试；不能将模拟行为测试作为这些场景的已通过证明。
- 本次不包含 computer-use Swift 修复，不能据此声称干净机器上原生构建已修复；原工作区的前序任务改动保留。
- 完整设置测试的基线失败需由模型设置测试维护者后续修正。升级 core、OTel、事件协议或前端设置渲染器时，应重跑上述相关消费者回归及真实环境验收。
- 本次只创建 PR，不执行合并。Maintainer 的正式设计及接口评审仍由仓库流程完成。
