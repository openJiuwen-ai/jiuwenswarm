---
title: D/F/B/S 文档总索引
author: sync_documents.py
generated: true
---

# D/F/B/S 文档总索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

按模块分组；跨模块文档在各相关分组展示。

## 模块导航

| 模块 |
|---|
| [agent_gateway/channel_manager/im_platforms/xiaoyi](#module-agent_gateway-channel_manager-im_platforms-xiaoyi) |
| [agent_gateway/message_handler](#module-agent_gateway-message_handler) |
| [agent_teams](#module-agent_teams) |
| [agent_teams/workflow](#module-agent_teams-workflow) |
| [deploy/yuanrong](#module-deploy-yuanrong) |
| [gateway/channel_manager/web](#module-gateway-channel_manager-web) |
| [jiuwenswarm](#module-jiuwenswarm) |
| [jiuwenswarm/agents/harness](#module-jiuwenswarm-agents-harness) |
| [jiuwenswarm/agents/harness/code/rails/heartbeat](#module-jiuwenswarm-agents-harness-code-rails-heartbeat) |
| [jiuwenswarm/agents/harness/common/prompt](#module-jiuwenswarm-agents-harness-common-prompt) |
| [jiuwenswarm/agents/harness/common/rails](#module-jiuwenswarm-agents-harness-common-rails) |
| [jiuwenswarm/agents/harness/common/recommendation](#module-jiuwenswarm-agents-harness-common-recommendation) |
| [jiuwenswarm/agents/harness/common/rsi](#module-jiuwenswarm-agents-harness-common-rsi) |
| [jiuwenswarm/agents/harness/common/tools](#module-jiuwenswarm-agents-harness-common-tools) |
| [jiuwenswarm/agents/harness/common/tools/cron](#module-jiuwenswarm-agents-harness-common-tools-cron) |
| [jiuwenswarm/agents/harness/team](#module-jiuwenswarm-agents-harness-team) |
| [jiuwenswarm/agents/harness/team/rails](#module-jiuwenswarm-agents-harness-team-rails) |
| [jiuwenswarm/agents/swarm](#module-jiuwenswarm-agents-swarm) |
| [jiuwenswarm/channels/tui](#module-jiuwenswarm-channels-tui) |
| [jiuwenswarm/channels/tui/frontend](#module-jiuwenswarm-channels-tui-frontend) |
| [jiuwenswarm/channels/web](#module-jiuwenswarm-channels-web) |
| [jiuwenswarm/channels/web/frontend](#module-jiuwenswarm-channels-web-frontend) |
| [jiuwenswarm/channels/web/frontend/src/features/rsi](#module-jiuwenswarm-channels-web-frontend-src-features-rsi) |
| [jiuwenswarm/channels/web/frontend/src/features/settings](#module-jiuwenswarm-channels-web-frontend-src-features-settings) |
| [jiuwenswarm/channels/web/frontend/src/features/settings/modules/models](#module-jiuwenswarm-channels-web-frontend-src-features-settings-modules-models) |
| [jiuwenswarm/common](#module-jiuwenswarm-common) |
| [jiuwenswarm/common/config_panel](#module-jiuwenswarm-common-config_panel) |
| [jiuwenswarm/extensions/sdk](#module-jiuwenswarm-extensions-sdk) |
| [jiuwenswarm/gateway/channel_manager/web](#module-jiuwenswarm-gateway-channel_manager-web) |
| [jiuwenswarm/observability](#module-jiuwenswarm-observability) |
| [jiuwenswarm/resources](#module-jiuwenswarm-resources) |
| [jiuwenswarm/resources/plugins/ttse](#module-jiuwenswarm-resources-plugins-ttse) |
| [jiuwenswarm/server](#module-jiuwenswarm-server) |
| [jiuwenswarm/server/control](#module-jiuwenswarm-server-control) |
| [jiuwenswarm/server/ipc](#module-jiuwenswarm-server-ipc) |
| [jiuwenswarm/server/runtime](#module-jiuwenswarm-server-runtime) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-jiuwenswarm-server-runtime-agent_adapter) |
| [jiuwenswarm/server/runtime/skill](#module-jiuwenswarm-server-runtime-skill) |
| [jiuwenswarm/server/supervisor](#module-jiuwenswarm-server-supervisor) |
| [jiuwenswarm/server/worker](#module-jiuwenswarm-server-worker) |
| [scripts](#module-scripts) |
| [tests](#module-tests) |
| [tests/fixtures/ttse_plugin](#module-tests-fixtures-ttse_plugin) |
| [tests/unit_tests/agentserver](#module-tests-unit_tests-agentserver) |
| [tests/unit_tests/gateway](#module-tests-unit_tests-gateway) |
| [tests/unit_tests/personal_context](#module-tests-unit_tests-personal_context) |
| [tests/unit_tests/server](#module-tests-unit_tests-server) |
| [tests/unit_tests/server/extensions](#module-tests-unit_tests-server-extensions) |
| [未分类](#module-unclassified) |

<a id="module-agent_gateway-channel_manager-im_platforms-xiaoyi"></a>

## agent_gateway/channel_manager/im_platforms/xiaoyi

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](bugfix/gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-agent_gateway-message_handler"></a>

## agent_gateway/message_handler

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](bugfix/gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-agent_teams"></a>

## agent_teams

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [SDD-0029](designs/proposals/SDD-0029-team-session-runtime-integration/design.md) | D | Team 模式接入统一 Session Runtime | @zhanglongmin | — |

<a id="module-agent_teams-workflow"></a>

## agent_teams/workflow

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [SDD-0009](designs/archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | D | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | @chenchunzhou | — |
| [SDD-0010](designs/archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | D | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | @chenchunzhou | — |
| [SDD-0028](designs/archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | D | swarmflow verify / fork 可观测与前端呈现 | @xiajing | — |

<a id="module-deploy-yuanrong"></a>

## deploy/yuanrong

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-gateway-channel_manager-web"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261031_1139_gateway-session-e2a](features/F_20261031_1139_gateway-session-e2a.md) | F | Web 会话管理统一经 E2A 交由 Server 执行 | ruiqiang2 | 无独立 D：用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。 |
| [B_20261102_1644_cron-web-default-project-id](bugfix/gateway/channel_manager/B_20261102_1644_cron-web-default-project-id.md) | B | 前端未选项目创建定时任务落库 project_id 回退为空串 | kdgmpw | 无独立 D：恢复 4b00671f6（#5713）已实现的行为，属于对该修复在企业版合入后被回退的局部回归修复，无独立设计。 |

<a id="module-jiuwenswarm"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [SDD-0009](designs/archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | D | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | @chenchunzhou | — |
| [SDD-0010](designs/archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | D | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | @chenchunzhou | — |
| [SDD-0028](designs/archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | D | swarmflow verify / fork 可观测与前端呈现 | @xiajing | — |
| [F_20261030_2018_teammate-swarmflow-adaptation](features/F_20261030_2018_teammate-swarmflow-adaptation.md) | F | 集群模式teammate支持swarmflow工具（jiuwenswarm 侧适配） | @xiajing | 无独立 D：本仓侧为 teammate 接入 swarmflow 的适配与硬化，无独立设计文档。 |
| [B_20261030_2020_pause-refires-consumed-popups](bugfix/jiuwenswarm/server/runtime/agent_adapter/B_20261030_2020_pause-refires-consumed-popups.md) | B | 暂停/取消后已消费的 human 弹窗被重放层重投 | xiajing | 无独立 D：重放层过滤由主干既有机制承载，本修复补触发层清场与消费层去重两段。 |
| [B_20261031_0946_ttse-codecheck-boundaries](bugfix/jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1053_atomgit-review](bugfix/jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |

<a id="module-jiuwenswarm-agents-harness"></a>

## jiuwenswarm/agents/harness

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-jiuwenswarm-agents-harness-code-rails-heartbeat"></a>

## jiuwenswarm/agents/harness/code/rails/heartbeat

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](bugfix/jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-jiuwenswarm-agents-harness-common-prompt"></a>

## jiuwenswarm/agents/harness/common/prompt

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |

<a id="module-jiuwenswarm-agents-harness-common-rails"></a>

## jiuwenswarm/agents/harness/common/rails

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | — |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |

<a id="module-jiuwenswarm-agents-harness-common-recommendation"></a>

## jiuwenswarm/agents/harness/common/recommendation

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |

<a id="module-jiuwenswarm-agents-harness-common-rsi"></a>

## jiuwenswarm/agents/harness/common/rsi

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261114_1302_terminal-metrics-and-model-errors](bugfix/jiuwenswarm/agents/harness/common/rsi/B_20261114_1302_terminal-metrics-and-model-errors.md) | B | RSI 终态统计、侧栏运行态和论文模型错误修复 | l_x_d | 无独立 D：按用户确认修复现有行为及 Issue 6558、6539、6532，无独立设计。 |
| [S_01_observation-projection](specs/jiuwenswarm/agents/harness/common/rsi/S_01_observation-projection.md) | S | RSI 进度、报告与前端终态投影 | l_x_d | — |

<a id="module-jiuwenswarm-agents-harness-common-tools"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | — |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |

<a id="module-jiuwenswarm-agents-harness-common-tools-cron"></a>

## jiuwenswarm/agents/harness/common/tools/cron

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](bugfix/jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-jiuwenswarm-agents-harness-team"></a>

## jiuwenswarm/agents/harness/team

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261112_1405_stop-btn-stuck-after-skip](bugfix/jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |

<a id="module-jiuwenswarm-agents-harness-team-rails"></a>

## jiuwenswarm/agents/harness/team/rails

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261110_1438_opencode-go-provider](features/F_20261110_1438_opencode-go-provider.md) | F | 新增 opencode-go 模型供应商 | wang_njust_608 | 无独立 D：需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。 |

<a id="module-jiuwenswarm-agents-swarm"></a>

## jiuwenswarm/agents/swarm

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261110_1438_opencode-go-provider](features/F_20261110_1438_opencode-go-provider.md) | F | 新增 opencode-go 模型供应商 | wang_njust_608 | 无独立 D：需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。 |
| [B_20261106_1132_organization-round-cleanup](bugfix/jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |

<a id="module-jiuwenswarm-channels-tui"></a>

## jiuwenswarm/channels/tui

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_1451_hub-default-url-consistency](bugfix/jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-jiuwenswarm-channels-tui-frontend"></a>

## jiuwenswarm/channels/tui/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261110_1438_opencode-go-provider](features/F_20261110_1438_opencode-go-provider.md) | F | 新增 opencode-go 模型供应商 | wang_njust_608 | 无独立 D：需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。 |

<a id="module-jiuwenswarm-channels-web"></a>

## jiuwenswarm/channels/web

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1146_chat-cold-load](bugfix/jiuwenswarm/channels/web/B_20261031_1146_chat-cold-load.md) | B | 会话嵌套路由冷加载白屏修复 | kdgmpw | 无独立 D：对已有静态服务契约的局部修复，与 nginx 的资源别名保持兼容，无独立设计。 |
| [B_20261102_1104_cron-stable-model-selection](bugfix/jiuwenswarm/channels/web/B_20261102_1104_cron-stable-model-selection.md) | B | 定时任务模型选择丢失稳定 ID 与执行失败报错不可见修复 | kdgmpw | 无独立 D：稳定选择契约（ModelPicker selection/onSelectionChange、CronJob.model_selection、 chat.error 去重窗口）均为既有实现，本次是 cron 抽屉这一旧契约调用方的迁移补接与 去重键缺维修复，无独立设计。 |
| [B_20261103_1112_raw-file-range-seek](bugfix/jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261103_1451_hub-default-url-consistency](bugfix/jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-jiuwenswarm-channels-web-frontend"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [SDD-0026](designs/proposals/SDD-0026-trajectory-observability-web-ui/design.md) | D | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | @alan_cheng | — |
| [F_20261029_1757_mcp-app-agent-stack-refresh](features/F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [F_20261110_1438_opencode-go-provider](features/F_20261110_1438_opencode-go-provider.md) | F | 新增 opencode-go 模型供应商 | wang_njust_608 | 无独立 D：需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。 |
| [B_20261030_1250_org-snapshot-removes-mirrored-workers](bugfix/jiuwenswarm/channels/web/frontend/B_20261030_1250_org-snapshot-removes-mirrored-workers.md) | B | 组织 UI 快照投影整表覆盖导致镜像 workflow worker 从成员面板消失 | xiajing | 无独立 D：对既有"快照完整替换"契约（跨 Team 切换不串数据）的成员级例外修复，属 F_20261027_1026 特性线的验证期 bugfix，无独立设计文档。 |
| [B_20261103_1112_raw-file-range-seek](bugfix/jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261107_1505_6226-forwarding-replay](bugfix/jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261112_1405_stop-btn-stuck-after-skip](bugfix/jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |
| [B_20261113_1416_cron-drawer-double-submit](bugfix/jiuwenswarm/channels/web/frontend/B_20261113_1416_cron-drawer-double-submit.md) | B | 定时任务抽屉确认按钮无提交中防护，慢响应窗口内双击创建重复任务 | gunner_beat | 无独立 D：交互层缺陷修复，复用同仓库 HeartbeatPanel 既有 submitting 机制，无独立设计文档。社区 issue 与修复 PR 为事实源：GitCode issue |
| [B_20261116_1701_my-plugin-installed-only](bugfix/jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |
| [S_01_app-agent-interaction](specs/mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | — |
| [S_01_plugin-marketplace](specs/jiuwenswarm/server/runtime/S_01_plugin-marketplace.md) | S | 插件市场列表归属与操作契约 | lizhiyuan | — |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |

<a id="module-jiuwenswarm-channels-web-frontend-src-features-rsi"></a>

## jiuwenswarm/channels/web/frontend/src/features/rsi

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261114_1302_terminal-metrics-and-model-errors](bugfix/jiuwenswarm/agents/harness/common/rsi/B_20261114_1302_terminal-metrics-and-model-errors.md) | B | RSI 终态统计、侧栏运行态和论文模型错误修复 | l_x_d | 无独立 D：按用户确认修复现有行为及 Issue 6558、6539、6532，无独立设计。 |
| [S_01_observation-projection](specs/jiuwenswarm/agents/harness/common/rsi/S_01_observation-projection.md) | S | RSI 进度、报告与前端终态投影 | l_x_d | — |

<a id="module-jiuwenswarm-channels-web-frontend-src-features-settings"></a>

## jiuwenswarm/channels/web/frontend/src/features/settings

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261110_2026_affinity-settings](features/F_20261110_2026_affinity-settings.md) | F | 实验功能算力亲和开关与自定义模型能力接入 | MengHan18 | 无独立 D：外部初始方案为 docs_HM_kvc/E前端开关打开/01 和 02；全局准入和交互按用户确认的 04 小白版文档替代，无仓库内 D 标识。 |
| [B_20261112_1716_affinity-status-read](bugfix/jiuwenswarm/server/control/B_20261112_1716_affinity-status-read.md) | B | 控制面遗漏亲和状态导致开关与说明不一致 | MengHan18 | 无独立 D：修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。 |
| [S_01_affinity-settings](specs/jiuwenswarm/common/kv_cache_affinity_config/S_01_affinity-settings.md) | S | 算力亲和设置与模型能力契约 | MengHan18 | — |

<a id="module-jiuwenswarm-channels-web-frontend-src-features-settings-modules-models"></a>

## jiuwenswarm/channels/web/frontend/src/features/settings/modules/models

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261116_1636_custom-model-reasoning-policy](bugfix/jiuwenswarm/channels/web/frontend/B_20261116_1636_custom-model-reasoning-policy.md) | B | 自定义模型误展示推理配置修复 | lizhiyuan | 无独立 D：用户已确认的既有设置页能力边界收敛；不新增模型发现或跨仓接口，属于局部回归修复。 |

<a id="module-jiuwenswarm-common"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261110_1438_opencode-go-provider](features/F_20261110_1438_opencode-go-provider.md) | F | 新增 opencode-go 模型供应商 | wang_njust_608 | 无独立 D：需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。 |
| [F_20261110_2026_affinity-settings](features/F_20261110_2026_affinity-settings.md) | F | 实验功能算力亲和开关与自定义模型能力接入 | MengHan18 | 无独立 D：外部初始方案为 docs_HM_kvc/E前端开关打开/01 和 02；全局准入和交互按用户确认的 04 小白版文档替代，无仓库内 D 标识。 |
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |
| [B_20261112_1716_affinity-status-read](bugfix/jiuwenswarm/server/control/B_20261112_1716_affinity-status-read.md) | B | 控制面遗漏亲和状态导致开关与说明不一致 | MengHan18 | 无独立 D：修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。 |
| [B_20261112_2025_permission-profile-refresh](bugfix/jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |
| [S_01_affinity-settings](specs/jiuwenswarm/common/kv_cache_affinity_config/S_01_affinity-settings.md) | S | 算力亲和设置与模型能力契约 | MengHan18 | — |

<a id="module-jiuwenswarm-common-config_panel"></a>

## jiuwenswarm/common/config_panel

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [F_20261110_1438_opencode-go-provider](features/F_20261110_1438_opencode-go-provider.md) | F | 新增 opencode-go 模型供应商 | wang_njust_608 | 无独立 D：需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。 |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |

<a id="module-jiuwenswarm-extensions-sdk"></a>

## jiuwenswarm/extensions/sdk

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [S_03_native-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_03_native-plugin-runtime.md) | S | 原生插件通用运行服务与生命周期 | xinyang_bali | — |

<a id="module-jiuwenswarm-gateway-channel_manager-web"></a>

## jiuwenswarm/gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | — |

<a id="module-jiuwenswarm-observability"></a>

## jiuwenswarm/observability

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | — |

<a id="module-jiuwenswarm-resources"></a>

## jiuwenswarm/resources

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-jiuwenswarm-resources-plugins-ttse"></a>

## jiuwenswarm/resources/plugins/ttse

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [S_02_ttse-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_ttse-plugin-runtime.md) | S | TTSE Coding 插件运行契约 | xinyang_bali | — |

<a id="module-jiuwenswarm-server"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261029_0927_missing-supervisor-entry](bugfix/jiuwenswarm/server/B_20261029_0927_missing-supervisor-entry.md) | B | 重启 AgentServer 时缺少 supervisor 启动函数 | xinyang_bali | 无独立 D：用户请求重启时发现当前工作区入口不完整，恢复既有 Front/Worker 启动契约。 |
| [B_20261031_1042_pr9317-review](bugfix/jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |

<a id="module-jiuwenswarm-server-control"></a>

## jiuwenswarm/server/control

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [B_20261112_1716_affinity-status-read](bugfix/jiuwenswarm/server/control/B_20261112_1716_affinity-status-read.md) | B | 控制面遗漏亲和状态导致开关与说明不一致 | MengHan18 | 无独立 D：修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。 |
| [B_20261112_2025_permission-profile-refresh](bugfix/jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |

<a id="module-jiuwenswarm-server-ipc"></a>

## jiuwenswarm/server/ipc

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261110_1042_issue6191-ipc-recovery](bugfix/jiuwenswarm/server/ipc/B_20261110_1042_issue6191-ipc-recovery.md) | B | IPC 突发积压与降级就绪重连导致 Runtime 失败 | 刘瑜 | 无独立 D：对现有有界 IPC 和 Runtime 恢复状态机的小范围修复，无独立设计。 |

<a id="module-jiuwenswarm-server-runtime"></a>

## jiuwenswarm/server/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261110_1438_opencode-go-provider](features/F_20261110_1438_opencode-go-provider.md) | F | 新增 opencode-go 模型供应商 | wang_njust_608 | 无独立 D：需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。 |
| [B_20261110_1849_group-lock-cancellation](bugfix/jiuwenswarm/server/runtime/B_20261110_1849_group-lock-cancellation.md) | B | 群任务取消期间等待文件锁清理完成 | ShowHsiang | 无独立 D：修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。 |
| [B_20261116_1701_my-plugin-installed-only](bugfix/jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |
| [S_01_plugin-marketplace](specs/jiuwenswarm/server/runtime/S_01_plugin-marketplace.md) | S | 插件市场列表归属与操作契约 | lizhiyuan | — |

<a id="module-jiuwenswarm-server-runtime-agent_adapter"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261029_1757_mcp-app-agent-stack-refresh](features/F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [F_20261110_1438_opencode-go-provider](features/F_20261110_1438_opencode-go-provider.md) | F | 新增 opencode-go 模型供应商 | wang_njust_608 | 无独立 D：需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。 |
| [B_20261031_0946_ttse-codecheck-boundaries](bugfix/jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1042_pr9317-review](bugfix/jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |
| [B_20261031_1053_atomgit-review](bugfix/jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |
| [B_20261106_1132_organization-round-cleanup](bugfix/jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |
| [S_01_app-agent-interaction](specs/mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | — |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | — |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |
| [S_03_native-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_03_native-plugin-runtime.md) | S | 原生插件通用运行服务与生命周期 | xinyang_bali | — |

<a id="module-jiuwenswarm-server-runtime-skill"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_0936_skill-action-param-type-validation](bugfix/jiuwenswarm/server/runtime/skill/B_20261103_0936_skill-action-param-type-validation.md) | B | 技能安装/更新/卸载入口参数类型校验缺失修复 | openjiuwen_ai | 无独立 D：三个入口的参数契约（string 标识、boolean force）已由前端 webHttpClient 与 EnterpriseSkillSourcePanel 既有调用形态声明，本次为入口 类型校验层缺失的补接，无独立设计。 |
| [B_20261116_1412_skill-import-validation-and-list](bugfix/jiuwenswarm/server/runtime/skill/B_20261116_1412_skill-import-validation-and-list.md) | B | 技能导入元数据与 ZIP 路径安全校验修复 | zhangzhongteng | 无独立 D：用户确认统一个人版技能导入和市场安装的元数据校验，并修复 ZIP 原始文件名及路径检查；无独立设计。 |

<a id="module-jiuwenswarm-server-supervisor"></a>

## jiuwenswarm/server/supervisor

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261107_1505_6226-forwarding-replay](bugfix/jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261110_1042_issue6191-ipc-recovery](bugfix/jiuwenswarm/server/ipc/B_20261110_1042_issue6191-ipc-recovery.md) | B | IPC 突发积压与降级就绪重连导致 Runtime 失败 | 刘瑜 | 无独立 D：对现有有界 IPC 和 Runtime 恢复状态机的小范围修复，无独立设计。 |

<a id="module-jiuwenswarm-server-worker"></a>

## jiuwenswarm/server/worker

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261105_1518_worker-dispatch-slot-accounting](bugfix/jiuwenswarm/server/worker/B_20261105_1518_worker-dispatch-slot-accounting.md) | B | Worker 派发容量排除已返回的 handler | kdgmpw | 无独立 D：按会话中确认的最小修复方案调整 Worker 内部计数。 |

<a id="module-scripts"></a>

## scripts

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | — |

<a id="module-tests"></a>

## tests

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [F_20261113_1040_del-ut-slim](features/F_20261113_1040_del-ut-slim.md) | F | develop 测试库瘦身第一批：删除 agent-gateway 镜像测试层与死代码副本 | kdgmpw | 无独立 D：基于会话内对 develop 测试库的两轮静态扫描（10 个分组深扫 + 17,316 个用例级 AST 指纹比对）与上游 agent-gateway 仓的逐字节校验，用户在会话中直接确认执行，无独立设计编号。 |

<a id="module-tests-fixtures-ttse_plugin"></a>

## tests/fixtures/ttse_plugin

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-tests-unit_tests-agentserver"></a>

## tests/unit_tests/agentserver

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [B_20261031_1048_ttse-core-dependent-ut](bugfix/tests/unit_tests/agentserver/B_20261031_1048_ttse-core-dependent-ut.md) | B | 暂时移除依赖未合入 core 工厂能力的插件 UT | xinyang_bali | 无独立 D：用户明确要求暂时删除依赖 core 的相关 UT，不调整依赖或运行实现，无独立 D。 |
| [B_20261116_1701_my-plugin-installed-only](bugfix/jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |

<a id="module-tests-unit_tests-gateway"></a>

## tests/unit_tests/gateway

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](bugfix/tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-tests-unit_tests-personal_context"></a>

## tests/unit_tests/personal_context

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](bugfix/tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-tests-unit_tests-server"></a>

## tests/unit_tests/server

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261110_1849_group-lock-cancellation](bugfix/jiuwenswarm/server/runtime/B_20261110_1849_group-lock-cancellation.md) | B | 群任务取消期间等待文件锁清理完成 | ShowHsiang | 无独立 D：修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。 |

<a id="module-tests-unit_tests-server-extensions"></a>

## tests/unit_tests/server/extensions

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-unclassified"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [README](designs/README.md) | D | 设计资产 | 待确认 | — |
| [REGISTRY](designs/REGISTRY.md) | D | SDD 编号台账 (REGISTRY) | 待确认 | — |
| [legacy-documents](designs/legacy-documents.md) | D | 旧格式设计资料 | 待确认 | — |
| [migration-20261026](designs/migration-20261026.md) | D | 设计文档迁移与跨仓引用 | 待确认 | — |
| [team_expert_group_manifest_design](designs/archive/team_expert_group_manifest_design.md) | D | Team 模式接入专家团 Manifest 加载设计 | 待确认 | — |
| [team_permission_design](designs/archive/team_permission_design.md) | D | Team 模式 Permissions 设计方案 | 待确认 | — |
| [team_plan_design](designs/archive/team_plan_design.md) | D | Team Plan 模式设计文档 | 待确认 | — |
| [team_worktree_isolation_agenttool_cn](designs/archive/team_worktree_isolation_agenttool_cn.md) | D | Team Teammate Worktree Isolation 设计文档 | 待确认 | — |
| [B_20261026_1602_code-multimodal-loading](bugfix/jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](bugfix/jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | 待核实 |
| [B_20261026_2035_gateway-contract-collection](bugfix/tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | 待核实 |
| [S_01_multimodal-tool-loading](specs/jiuwenswarm/server/runtime/agent_adapter/S_01_multimodal-tool-loading.md) | S | Work / Code 多模态工具加载契约 | 待确认 | — |
