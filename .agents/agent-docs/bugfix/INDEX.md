---
title: 故障修复 B索引
author: sync_documents.py
generated: true
---

# 故障修复 B索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

按模块分组；跨模块文档在各相关分组展示。

## 模块导航

| 模块 |
|---|
| [agent-gateway/channel_manager/web](#module-agent-gateway-channel_manager-web) |
| [agent-gateway/extensions/runtime_management_extension](#module-agent-gateway-extensions-runtime_management_extension) |
| [agent_gateway/channel_manager/im_platforms/xiaoyi](#module-agent_gateway-channel_manager-im_platforms-xiaoyi) |
| [agent_gateway/message_handler](#module-agent_gateway-message_handler) |
| [gateway/channel_manager/web](#module-gateway-channel_manager-web) |
| [jiuwenswarm](#module-jiuwenswarm) |
| [jiuwenswarm/agents/harness](#module-jiuwenswarm-agents-harness) |
| [jiuwenswarm/agents/harness/code/rails/heartbeat](#module-jiuwenswarm-agents-harness-code-rails-heartbeat) |
| [jiuwenswarm/agents/harness/common/rails](#module-jiuwenswarm-agents-harness-common-rails) |
| [jiuwenswarm/agents/harness/common/recommendation](#module-jiuwenswarm-agents-harness-common-recommendation) |
| [jiuwenswarm/agents/harness/common/rsi](#module-jiuwenswarm-agents-harness-common-rsi) |
| [jiuwenswarm/agents/harness/common/tools](#module-jiuwenswarm-agents-harness-common-tools) |
| [jiuwenswarm/agents/harness/common/tools/cron](#module-jiuwenswarm-agents-harness-common-tools-cron) |
| [jiuwenswarm/agents/harness/team](#module-jiuwenswarm-agents-harness-team) |
| [jiuwenswarm/agents/swarm](#module-jiuwenswarm-agents-swarm) |
| [jiuwenswarm/channels/desktop](#module-jiuwenswarm-channels-desktop) |
| [jiuwenswarm/channels/desktop/electron](#module-jiuwenswarm-channels-desktop-electron) |
| [jiuwenswarm/channels/tui](#module-jiuwenswarm-channels-tui) |
| [jiuwenswarm/channels/web](#module-jiuwenswarm-channels-web) |
| [jiuwenswarm/channels/web/frontend](#module-jiuwenswarm-channels-web-frontend) |
| [jiuwenswarm/channels/web/frontend/src/components/SkillPanel](#module-jiuwenswarm-channels-web-frontend-src-components-SkillPanel) |
| [jiuwenswarm/channels/web/frontend/src/components/ui/EntityHeader](#module-jiuwenswarm-channels-web-frontend-src-components-ui-EntityHeader) |
| [jiuwenswarm/channels/web/frontend/src/features/rsi](#module-jiuwenswarm-channels-web-frontend-src-features-rsi) |
| [jiuwenswarm/channels/web/frontend/src/features/settings](#module-jiuwenswarm-channels-web-frontend-src-features-settings) |
| [jiuwenswarm/channels/web/frontend/src/features/settings/modules/models](#module-jiuwenswarm-channels-web-frontend-src-features-settings-modules-models) |
| [jiuwenswarm/common](#module-jiuwenswarm-common) |
| [jiuwenswarm/common/config_panel](#module-jiuwenswarm-common-config_panel) |
| [jiuwenswarm/debug_launcher.py](#module-jiuwenswarm-debug_launcher-py) |
| [jiuwenswarm/enterprise/config](#module-jiuwenswarm-enterprise-config) |
| [jiuwenswarm/enterprise/team](#module-jiuwenswarm-enterprise-team) |
| [jiuwenswarm/extensions/app_market](#module-jiuwenswarm-extensions-app_market) |
| [jiuwenswarm/extensions/runtime_management_extension](#module-jiuwenswarm-extensions-runtime_management_extension) |
| [jiuwenswarm/observability](#module-jiuwenswarm-observability) |
| [jiuwenswarm/resources](#module-jiuwenswarm-resources) |
| [jiuwenswarm/extensions/runtime_management_extension](#module-jiuwenswarm-extensions-runtime_management_extension) |
| [jiuwenswarm/resources/agent/workspace/skills](#module-jiuwenswarm-resources-agent-workspace-skills) |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin](#module-jiuwenswarm-resources-agent-workspace-skills-workswarm-builtin) |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/advanced-daily-report](#module-jiuwenswarm-resources-agent-workspace-skills-workswarm-builtin-references-capabilities-advanced-daily-report) |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder](#module-jiuwenswarm-resources-agent-workspace-skills-workswarm-builtin-references-capabilities-workswarm-app-builder) |
| [jiuwenswarm/runtime](#module-jiuwenswarm-runtime) |
| [jiuwenswarm/server](#module-jiuwenswarm-server) |
| [jiuwenswarm/server/control](#module-jiuwenswarm-server-control) |
| [jiuwenswarm/server/control/store](#module-jiuwenswarm-server-control-store) |
| [jiuwenswarm/server/ipc](#module-jiuwenswarm-server-ipc) |
| [jiuwenswarm/server/personal_context](#module-jiuwenswarm-server-personal_context) |
| [jiuwenswarm/server/runtime](#module-jiuwenswarm-server-runtime) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-jiuwenswarm-server-runtime-agent_adapter) |
| [jiuwenswarm/server/runtime/marketplace](#module-jiuwenswarm-server-runtime-marketplace) |
| [jiuwenswarm/server/runtime/session](#module-jiuwenswarm-server-runtime-session) |
| [jiuwenswarm/server/runtime/skill](#module-jiuwenswarm-server-runtime-skill) |
| [jiuwenswarm/server/supervisor](#module-jiuwenswarm-server-supervisor) |
| [jiuwenswarm/server/worker](#module-jiuwenswarm-server-worker) |
| [scripts](#module-scripts) |
| [tests/fixtures/ttse_plugin](#module-tests-fixtures-ttse_plugin) |
| [tests/unit_tests/agentserver](#module-tests-unit_tests-agentserver) |
| [tests/unit_tests/gateway](#module-tests-unit_tests-gateway) |
| [tests/unit_tests/personal_context](#module-tests-unit_tests-personal_context) |
| [tests/unit_tests/server](#module-tests-unit_tests-server) |
| [tests/unit_tests/server/extensions](#module-tests-unit_tests-server-extensions) |
| [未分类](#module-unclassified) |

<a id="module-agent-gateway-channel_manager-web"></a>

## agent-gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261124_1704_skills-uninstall-missing-name-400](extensions/B_20261124_1704_skills-uninstall-missing-name-400.md) | B | 企业版 Skill 卸载缺 name/null 返回 HTTP 500 改为 400 | meizi6 | 无独立 D：无独立 D；依据现有北向 POST /skills/actions/uninstall 与南向 DELETE /skills/{name} 契约，将路径占位符缺失归类为客户端入参错误。 |

<a id="module-agent-gateway-extensions-runtime_management_extension"></a>

## agent-gateway/extensions/runtime_management_extension

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261124_1704_skills-uninstall-missing-name-400](extensions/B_20261124_1704_skills-uninstall-missing-name-400.md) | B | 企业版 Skill 卸载缺 name/null 返回 HTTP 500 改为 400 | meizi6 | 无独立 D：无独立 D；依据现有北向 POST /skills/actions/uninstall 与南向 DELETE /skills/{name} 契约，将路径占位符缺失归类为客户端入参错误。 |

<a id="module-agent_gateway-channel_manager-im_platforms-xiaoyi"></a>

## agent_gateway/channel_manager/im_platforms/xiaoyi

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-agent_gateway-message_handler"></a>

## agent_gateway/message_handler

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-gateway-channel_manager-web"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261102_1644_cron-web-default-project-id](gateway/channel_manager/B_20261102_1644_cron-web-default-project-id.md) | B | 前端未选项目创建定时任务落库 project_id 回退为空串 | kdgmpw | 无独立 D：恢复 4b00671f6（#5713）已实现的行为，属于对该修复在企业版合入后被回退的局部回归修复，无独立设计。 |

<a id="module-jiuwenswarm"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261030_2020_pause-refires-consumed-popups](jiuwenswarm/server/runtime/agent_adapter/B_20261030_2020_pause-refires-consumed-popups.md) | B | 暂停/取消后已消费的 human 弹窗被重放层重投 | xiajing | 无独立 D：重放层过滤由主干既有机制承载，本修复补触发层清场与消费层去重两段。 |
| [B_20261031_0946_ttse-codecheck-boundaries](jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1053_atomgit-review](jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |

<a id="module-jiuwenswarm-agents-harness"></a>

## jiuwenswarm/agents/harness

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261124_1602_issue-4327-observability](observability/B_20261124_1602_issue-4327-observability.md) | B | issue 4327：统一可观测性配置并分离轨迹存储与导出 | hh0205 | 无独立 D：依据 GitCode jiuwenswarm issue 4327，统一 observability 开关并分离轨迹存储、演进采集和可观测性导出的职责，无独立 D 文档。 |
| [B_20261124_1717_main-agent-template-trace](jiuwenswarm/server/runtime/agent_adapter/B_20261124_1717_main-agent-template-trace.md) | B | 企业版 Work、Code 的主 Agent Trace 显示默认名 | 垚垚 | 无独立 D：本次修复企业版 Work、Code 主 Agent 的 Trace 展示名，沿用现有会话配置与观测接口，无独立设计文档。 |

<a id="module-jiuwenswarm-agents-harness-code-rails-heartbeat"></a>

## jiuwenswarm/agents/harness/code/rails/heartbeat

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-jiuwenswarm-agents-harness-common-rails"></a>

## jiuwenswarm/agents/harness/common/rails

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261117_2001_deferred-memory-failure](jiuwenswarm/agents/harness/common/rails/B_20261117_2001_deferred-memory-failure.md) | B | 延迟调用记忆工具时传播失败状态 | ToNy_St4rk | 无独立 D：修复 Issue 6151 中已有工具结果契约的状态传播，无独立设计。 |
| [B_20261126_1625_enterprise-cron-permission-rail-a4p-decouple](jiuwenswarm/agents/harness/common/rails/B_20261126_1625_enterprise-cron-permission-rail-a4p-decouple.md) | B | 企业版 Cron 权限护栏与 A4P 解耦，ask 无人值守 fail-closed | zhangxiangyu52 | 无独立 D：无独立 D：依据企业版安全护栏与定时任务无人值守契约的既有产品要求及 PR |

<a id="module-jiuwenswarm-agents-harness-common-recommendation"></a>

## jiuwenswarm/agents/harness/common/recommendation

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261126_1045_ui-lang-over-yaml-zh](jiuwenswarm/enterprise/config/B_20261126_1045_ui-lang-over-yaml-zh.md) | B | 企业版 UI 语言被 AgentServer yaml preferred_language zh 盖过 | whds5279 | 无独立 D：无独立 D；依据 issue |

<a id="module-jiuwenswarm-agents-harness-common-rsi"></a>

## jiuwenswarm/agents/harness/common/rsi

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261114_1302_terminal-metrics-and-model-errors](jiuwenswarm/agents/harness/common/rsi/B_20261114_1302_terminal-metrics-and-model-errors.md) | B | RSI 终态统计、侧栏运行态和论文模型错误修复 | l_x_d | 无独立 D：按用户确认修复现有行为及 Issue 6558、6539、6532，无独立设计。 |
| [B_20261119_1104_paper-snapshot-restart](jiuwenswarm/agents/harness/common/rsi/B_20261119_1104_paper-snapshot-restart.md) | B | 论文优化历史快照的重启恢复 | l_x_d | 无独立 D：按确认的重启恢复用例修复既有行为，无独立设计。 |

<a id="module-jiuwenswarm-agents-harness-common-tools"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261126_0956_spreadsheet-out-of-range-paging](jiuwenswarm/agents/harness/common/tools/B_20261126_0956_spreadsheet-out-of-range-paging.md) | B | read_spreadsheet 越界分页与仅表头文件的矛盾提示 | openjiuwen_ai | 无独立 D：内置工具只读分页提示的缺陷修复，无独立设计方案，按 Issue |
| [B_20261126_0210_enterprise-team-file-live-delivery](jiuwenswarm/agents/harness/common/tools/B_20261126_0210_enterprise-team-file-live-delivery.md) | B | 企业版团队会话文件卡片丢失：投递优先级反转与网关补发 | hanxinyang | 无独立 D：针对 Issue |

<a id="module-jiuwenswarm-agents-harness-common-tools-cron"></a>

## jiuwenswarm/agents/harness/common/tools/cron

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-jiuwenswarm-agents-harness-team"></a>

## jiuwenswarm/agents/harness/team

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261112_1405_stop-btn-stuck-after-skip](jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |
| [B_20261121_1545_team-pause-teardown-admission-race](jiuwenswarm/B_20261121_1545_team-pause-teardown-admission-race.md) | B | 团队暂停双执行与收尾窗口新消息误投递：回合交接竞态 | zhanglongmin | 无独立 D：依据运行日志时间线与代码逐跳核实定位，无独立设计文档；修复范围限定回合交接的暂停单入口、统一收尾屏障与路由守卫，不改变 Core 侧收尾顺序。 |
| [B_20261121_1817_parked-team-busy-snapshot-reseed](jiuwenswarm/B_20261121_1817_parked-team-busy-snapshot-reseed.md) | B | 暂停后页面持续转圈：挂起团队忙闲快照读到内存名册残留 | zhanglongmin | 无独立 D：依据运行日志与逐跳代码核实定位，无独立设计文档；修复走 Core 既有公开契约，不修改 agent-core。 |
| [B_20261126_1045_ui-lang-over-yaml-zh](jiuwenswarm/enterprise/config/B_20261126_1045_ui-lang-over-yaml-zh.md) | B | 企业版 UI 语言被 AgentServer yaml preferred_language zh 盖过 | whds5279 | 无独立 D：无独立 D；依据 issue |

<a id="module-jiuwenswarm-agents-swarm"></a>

## jiuwenswarm/agents/swarm

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261106_1132_organization-round-cleanup](jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |
| [B_20261126_1625_enterprise-cron-permission-rail-a4p-decouple](jiuwenswarm/agents/harness/common/rails/B_20261126_1625_enterprise-cron-permission-rail-a4p-decouple.md) | B | 企业版 Cron 权限护栏与 A4P 解耦，ask 无人值守 fail-closed | zhangxiangyu52 | 无独立 D：无独立 D：依据企业版安全护栏与定时任务无人值守契约的既有产品要求及 PR |

<a id="module-jiuwenswarm-channels-desktop"></a>

## jiuwenswarm/channels/desktop

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261119_1416_builtin-extension-discovery](jiuwenswarm/extensions/app_market/B_20261119_1416_builtin-extension-discovery.md) | B | 本地默认启动未加载 Swarm 内置扩展导致 app_market RPC 不可用 | kdgmpw | 无独立 D：本次为本地启动装配缺陷修复，依据现有应用市场扩展契约与 Gateway 扩展加载实现，无独立设计。 |

<a id="module-jiuwenswarm-channels-desktop-electron"></a>

## jiuwenswarm/channels/desktop/electron

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261126_1634_desktop-app-permissions](jiuwenswarm/channels/desktop/electron/B_20261126_1634_desktop-app-permissions.md) | B | 桌面应用鼠标锁定与全屏权限 | kdgmpw | 无独立 D：局部修复以应用市场分端展示契约和网页版已有权限为依据。 |

<a id="module-jiuwenswarm-channels-tui"></a>

## jiuwenswarm/channels/tui

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_1451_hub-default-url-consistency](jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-jiuwenswarm-channels-web"></a>

## jiuwenswarm/channels/web

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1146_chat-cold-load](jiuwenswarm/channels/web/B_20261031_1146_chat-cold-load.md) | B | 会话嵌套路由冷加载白屏修复 | kdgmpw | 无独立 D：对已有静态服务契约的局部修复，与 nginx 的资源别名保持兼容，无独立设计。 |
| [B_20261102_1104_cron-stable-model-selection](jiuwenswarm/channels/web/B_20261102_1104_cron-stable-model-selection.md) | B | 定时任务模型选择丢失稳定 ID 与执行失败报错不可见修复 | kdgmpw | 无独立 D：稳定选择契约（ModelPicker selection/onSelectionChange、CronJob.model_selection、 chat.error 去重窗口）均为既有实现，本次是 cron 抽屉这一旧契约调用方的迁移补接与 去重键缺维修复，无独立设计。 |
| [B_20261103_1112_raw-file-range-seek](jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261103_1451_hub-default-url-consistency](jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |
| [B_20261117_1019_skill-upload-temp-leak](jiuwenswarm/server/runtime/skill/B_20261117_1019_skill-upload-temp-leak.md) | B | 技能导入的两阶段上传临时目录永久残留 | mengliang0116 | 无独立 D：两阶段上传缺少文件生命周期归属，属实现缺陷的小范围修复，不引入新的架构设计；接口与生命周期契约记录在本文。 |
| [B_20261124_1943_personal-context-failure-detail](jiuwenswarm/server/B_20261124_1943_personal-context-failure-detail.md) | B | 个人知识空间采集失败只显示「采集失败」，无法定位失败阶段与失败文件 | openjiuwen_ai | 无独立 D：只修采集失败诊断信息的透出与展示，不改变采集调度、存储契约和授权流程；无独立设计。 |

<a id="module-jiuwenswarm-channels-web-frontend"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261030_1250_org-snapshot-removes-mirrored-workers](jiuwenswarm/channels/web/frontend/B_20261030_1250_org-snapshot-removes-mirrored-workers.md) | B | 组织 UI 快照投影整表覆盖导致镜像 workflow worker 从成员面板消失 | xiajing | 无独立 D：对既有"快照完整替换"契约（跨 Team 切换不串数据）的成员级例外修复，属 F_20261027_1026 特性线的验证期 bugfix，无独立设计文档。 |
| [B_20261103_1112_raw-file-range-seek](jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261105_0941_app-stop-graceful-and-page-close](jiuwenswarm/extensions/app_market/app-stop-graceful-and-page-close.md) | B | 应用市场停止强杀跳过应用清理与页面关闭无感知 | kdgmpw | 无独立 D：修复依据既有契约（app-builder SKILL.md 的"应用负责优雅退出和会话清理、市场只终止进程"与扩展 README 的运行描述），无独立 D；新增契约已固化为 specs/jiuwenswarm/extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md。 |
| [B_20261105_1500_start-completion-steals-view](jiuwenswarm/extensions/app_market/start-completion-steals-view.md) | B | 应用启动完成抢走用户后来切换的页面 | kdgmpw | 无独立 D：依据 S_01_app-market-browser-containers 与 S_02_app-market-stop-and-page-lifecycle 的“按稳定 app_id 复用页面、失败/停止不误关其他页面”契约；本次修复补充用户意图排序，无独立 D。 |
| [B_20261107_1505_6226-forwarding-replay](jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261112_1405_stop-btn-stuck-after-skip](jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |
| [B_20261113_1416_cron-drawer-double-submit](jiuwenswarm/channels/web/frontend/B_20261113_1416_cron-drawer-double-submit.md) | B | 定时任务抽屉确认按钮无提交中防护，慢响应窗口内双击创建重复任务 | gunner_beat | 无独立 D：交互层缺陷修复，复用同仓库 HeartbeatPanel 既有 submitting 机制，无独立设计文档。社区 issue 与修复 PR 为事实源：GitCode issue |
| [B_20261116_1652_cli-auth-effect-loop](jiuwenswarm/channels/web/B_20261116_1652_cli-auth-effect-loop.md) | B | CLI MCP 授权因父级重渲染重复开窗和请求 | TwZhang | 无独立 D：用户明确授权修复现有 CLI 授权生命周期缺陷，无独立设计。 |
| [B_20261116_1701_my-plugin-installed-only](jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |
| [B_20261117_1522_team-plan-toggle-lock](jiuwenswarm/B_20261117_1522_team-plan-toggle-lock.md) | B | 集群会话计划模式开关是假开关：建队后冻结并让位动态工作流 | chenchunzhou | 无独立 D：由 issue 6361 现象逆向定位，无独立设计文档；方案与取舍记录在 OpenSpec 变更 issue-6361-team-plan-toggle-lock 的 proposal/design/specs，修复范围限定 Web 前端开关闸门与提示文案。 |
| [B_20261118_1055_worker-close-after-final-error](jiuwenswarm/channels/web/frontend/B_20261118_1055_worker-close-after-final-error.md) | B | 任务正常收尾后 worker 关闭被渲染为对话错误 | openjiuwen_ai | 无独立 D：只在 Web 前端区分「本轮已收尾」与「处理中失败」，不改 supervisor/gateway 的错误码与生命周期协议；无独立设计。 |
| [B_20261118_1527_session-delete-plan-runtime](jiuwenswarm/channels/web/frontend/B_20261118_1527_session-delete-plan-runtime.md) | B | 侧边栏删除会话漏清 planStore runtime，Plan 模式状态在内存永久残留 | gunner_beat | 无独立 D：会话生命周期清理的一致性补齐，复用 usePlanStore 既有 removeRuntime，无独立设计文档。缺陷检视清单 |
| [B_20261119_1039_video-memory-submit-pending](jiuwenswarm/channels/web/frontend/B_20261119_1039_video-memory-submit-pending.md) | B | 视频记忆任务 pending 导致提交工具持续执行中 | ToNy_St4rk | 无独立 D：现有工具结果状态的局部修复，无独立设计。 |
| [B_20261119_1150_chat-terminal-reasoning-close](jiuwenswarm/channels/web/frontend/B_20261119_1150_chat-terminal-reasoning-close.md) | B | 消息出错后思考段不收口，「思考中…」永久转圈直到下次发消息 | gunner_beat | 无独立 D：交互层缺陷修复，复用同文件 chat.processing_status(false) 分支已有的「终态兜底收口」口径（幂等空操作），无独立设计文档。 |
| [B_20261121_1529_skill-install-toast-display-name-width](jiuwenswarm/channels/web/frontend/B_20261121_1529_skill-install-toast-display-name-width.md) | B | 技能安装成功提示显示英文 slug 与提示条固定过宽修复 | yuhaiquan | 无独立 D：由 issue |
| [B_20261125_0115_cron-pin-sidebar-blackhole](jiuwenswarm/server/runtime/session/B_20261125_0115_cron-pin-sidebar-blackhole.md) | B | 缺 cron_id 的 cron 执行会话置顶后从侧边栏所有区域消失且无法取消置顶 | CharlesWang2030 | 无独立 D：由缺陷分析归档 JW-78 逆向定位，无独立设计文档；方案取舍（入口拒绝 vs 准入放宽 vs 元数据自愈）记录在本文档决策节。 |
| [B_20261125_1146_design-shared-path-regressions](jiuwenswarm/channels/web/B_20261125_1146_design-shared-path-regressions.md) | B | Design 公共路径历史回归防护 | Huang, Zeyu | 无独立 D：依据历史 issue 和当前公共路径契约补回归测试，无独立设计方案；不重构产品代码。 |
| [B_20261126_1420_channel-settings-failure-feedback](jiuwenswarm/channels/web/frontend/B_20261126_1420_channel-settings-failure-feedback.md) | B | 渠道设置页保存/加载失败反馈缺失——保存失败零提示、单渠道加载失败整列表永久骨架 | gunner_beat | 无独立 D：交互反馈层缺陷修复，复用 FormDialog 既有 status 插槽与骨架/横幅/toast 三层反馈位，无独立设计文档。社区 issue 与修复 PR 为事实源：GitCode issue |
| [B_20261126_2000_agent-mgmt-confirm-dialog-i18n](jiuwenswarm/channels/web/frontend/B_20261126_2000_agent-mgmt-confirm-dialog-i18n.md) | B | 专家团卸载与专家删除确认框原生 window.confirm 按钮文案无法随应用 i18n 切换 | whds5279 | 无独立 D：交互层缺陷修复，复用既有 ConfirmDialog 组件与 agentManagement.confirm / common.cancel\|confirm\|loading 文案契约，无独立设计文档。社区 issue 与修复 PR 为事实源：GitCode issue |
| [B_20261126_1628_upload-doc-block-auto-title](jiuwenswarm/B_20261126_1628_upload-doc-block-auto-title.md) | B | 会话自动标题被【上传文档】协议块污染 | whds5279 | 无独立 D：修复既有标题生成与上传协议块注入的跨层契约冲突，不引入新方案。 |
| [B_20261126_1940_enterprise-agent-switch-route](jiuwenswarm/channels/web/B_20261126_1940_enterprise-agent-switch-route.md) | B | Open a new conversation after an enterprise Agent switch | linda | 无独立 D：Minimal fix explicitly approved in the task; no separate design document. |

<a id="module-jiuwenswarm-channels-web-frontend-src-components-SkillPanel"></a>

## jiuwenswarm/channels/web/frontend/src/components/SkillPanel

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261121_1529_skill-install-toast-display-name-width](jiuwenswarm/channels/web/frontend/B_20261121_1529_skill-install-toast-display-name-width.md) | B | 技能安装成功提示显示英文 slug 与提示条固定过宽修复 | yuhaiquan | 无独立 D：由 issue |

<a id="module-jiuwenswarm-channels-web-frontend-src-components-ui-EntityHeader"></a>

## jiuwenswarm/channels/web/frontend/src/components/ui/EntityHeader

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261120_1024_entity-header-tag-overflow](jiuwenswarm/channels/web/frontend/B_20261120_1024_entity-header-tag-overflow.md) | B | 实体头部标签溢出处理 | 待确认 | 无独立 D：按用户确认的 jiuwenclaw 修复方案迁入同源缺陷修复，无独立设计文档。 |

<a id="module-jiuwenswarm-channels-web-frontend-src-features-rsi"></a>

## jiuwenswarm/channels/web/frontend/src/features/rsi

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261114_1302_terminal-metrics-and-model-errors](jiuwenswarm/agents/harness/common/rsi/B_20261114_1302_terminal-metrics-and-model-errors.md) | B | RSI 终态统计、侧栏运行态和论文模型错误修复 | l_x_d | 无独立 D：按用户确认修复现有行为及 Issue 6558、6539、6532，无独立设计。 |

<a id="module-jiuwenswarm-channels-web-frontend-src-features-settings"></a>

## jiuwenswarm/channels/web/frontend/src/features/settings

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261112_1716_affinity-status-read](jiuwenswarm/server/control/B_20261112_1716_affinity-status-read.md) | B | 控制面遗漏亲和状态导致开关与说明不一致 | MengHan18 | 无独立 D：修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。 |

<a id="module-jiuwenswarm-channels-web-frontend-src-features-settings-modules-models"></a>

## jiuwenswarm/channels/web/frontend/src/features/settings/modules/models

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261116_1636_custom-model-reasoning-policy](jiuwenswarm/channels/web/frontend/B_20261116_1636_custom-model-reasoning-policy.md) | B | 自定义模型误展示推理配置修复 | lizhiyuan | 无独立 D：用户已确认的既有设置页能力边界收敛；不新增模型发现或跨仓接口，属于局部回归修复。 |
| [B_20261117_1727_model-output-token-limit](jiuwenswarm/channels/web/frontend/B_20261117_1727_model-output-token-limit.md) | B | 模型单次最大输出配置修复 | lizhiyuan | 无独立 D：用户在本轮会话确认的模型设置范围调整，未形成独立设计文档。 |

<a id="module-jiuwenswarm-common"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [B_20261112_1716_affinity-status-read](jiuwenswarm/server/control/B_20261112_1716_affinity-status-read.md) | B | 控制面遗漏亲和状态导致开关与说明不一致 | MengHan18 | 无独立 D：修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。 |
| [B_20261112_2025_permission-profile-refresh](jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |
| [B_20261119_1416_builtin-extension-discovery](jiuwenswarm/extensions/app_market/B_20261119_1416_builtin-extension-discovery.md) | B | 本地默认启动未加载 Swarm 内置扩展导致 app_market RPC 不可用 | kdgmpw | 无独立 D：本次为本地启动装配缺陷修复，依据现有应用市场扩展契约与 Gateway 扩展加载实现，无独立设计。 |
| [B_20261120_1735_upsert-empty-temperature](jiuwenswarm/common/B_20261120_1735_upsert-empty-temperature.md) | B | models.upsert 将空串 temperature 原样落库，与文档约定的空串转 null 不符 | openjiuwen_ai | 无独立 D：仅统一 upsert 与 replace_all 对空串 temperature 的归一化口径，不改模型配置结构与合并语义；无独立设计。 |
| [B_20261120_2339_singleton-model-is-default](jiuwenswarm/common/B_20261120_2339_singleton-model-is-default.md) | B | models.upsert 对单条 model_name 强制 is_default=true，请求 false 被静默改写 | openjiuwen_ai | 无独立 D：仅调整 is_default 的读取推断规则，不改变写入契约、模型分组结构与选择兜底；无独立设计。 |
| [B_20261121_0108_pypi-update-version-baseline](jiuwenswarm/common/B_20261121_0108_pypi-update-version-baseline.md) | B | 更新检查用打包源版本比较、并展示非最新版本的 whl，导致每日构建显示可升级到更旧的版本 | openjiuwen_ai | 无独立 D：仅修正更新检查的版本基线取值与 PyPI 源对最新版本文件的归属判定，不改变更新源配置、安装方式与前端展示契约；无独立设计。 |
| [B_20261124_1602_issue-4327-observability](observability/B_20261124_1602_issue-4327-observability.md) | B | issue 4327：统一可观测性配置并分离轨迹存储与导出 | hh0205 | 无独立 D：依据 GitCode jiuwenswarm issue 4327，统一 observability 开关并分离轨迹存储、演进采集和可观测性导出的职责，无独立 D 文档。 |
| [B_20261124_2201_rea-npx-readiness](jiuwenswarm/common/B_20261124_2201_rea-npx-readiness.md) | B | 系统 Node 安装后 REA 被误判未就绪并反复回到一键安装 | xiaotian077 | 无独立 D：对既有 REA 安装就绪判定的局部缺陷修复，不新增架构方案。 |
| [B_20261125_2150_windows-installer-restore-bootstrap](scripts/B_20261125_2150_windows-installer-restore-bootstrap.md) | B | Windows 安装器恢复进程隔离初始化与分派测试修正 | XinchenZzz | 无独立 D：对 Issue 7056 已有恢复事务的局部故障修复及测试适配，没有新增独立设计。 |
| [B_20261125_1146_design-shared-path-regressions](jiuwenswarm/channels/web/B_20261125_1146_design-shared-path-regressions.md) | B | Design 公共路径历史回归防护 | Huang, Zeyu | 无独立 D：依据历史 issue 和当前公共路径契约补回归测试，无独立设计方案；不重构产品代码。 |
| [B_20261126_1631_shared-workspace-dir-metadata](jiuwenswarm/common/B_20261126_1631_shared-workspace-dir-metadata.md) | B | 共享卷上工作区准备因已存在目录元数据 EPERM 导致 AgentServer 启动失败 | wangmingqi | 无独立 D：对既有工作区模板播种行为的局部缺陷修复与语义收紧，不新增架构方案。 |

<a id="module-jiuwenswarm-common-config_panel"></a>

## jiuwenswarm/common/config_panel

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261117_1727_model-output-token-limit](jiuwenswarm/channels/web/frontend/B_20261117_1727_model-output-token-limit.md) | B | 模型单次最大输出配置修复 | lizhiyuan | 无独立 D：用户在本轮会话确认的模型设置范围调整，未形成独立设计文档。 |

<a id="module-jiuwenswarm-debug_launcher-py"></a>

## jiuwenswarm/debug_launcher.py

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261120_1958_debug-worker-shutdown-budget](jiuwenswarm/B_20261120_1958_debug-worker-shutdown-budget.md) | B | Debug 重启提前终止正常清理中的 Worker | alan_cheng | 无独立 D：用户确认经真实子进程对照验证的局部修复；保留已有生命周期和身份保护，无独立设计文档。 |

<a id="module-jiuwenswarm-enterprise-config"></a>

## jiuwenswarm/enterprise/config

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261126_1045_ui-lang-over-yaml-zh](jiuwenswarm/enterprise/config/B_20261126_1045_ui-lang-over-yaml-zh.md) | B | 企业版 UI 语言被 AgentServer yaml preferred_language zh 盖过 | whds5279 | 无独立 D：无独立 D；依据 issue |
<a id="module-jiuwenswarm-enterprise-team"></a>

## jiuwenswarm/enterprise/team

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261126_0210_enterprise-team-file-live-delivery](jiuwenswarm/agents/harness/common/tools/B_20261126_0210_enterprise-team-file-live-delivery.md) | B | 企业版团队会话文件卡片丢失：投递优先级反转与网关补发 | hanxinyang | 无独立 D：针对 Issue |
| [B_20261126_0212_team-delete-partial-quota-release](jiuwenswarm/server/B_20261126_0212_team-delete-partial-quota-release.md) | B | 团队部分删除后配额占用残留：按会话清理与实体保留 | hanxinyang | 无独立 D：针对 Issue |

<a id="module-jiuwenswarm-extensions-app_market"></a>

## jiuwenswarm/extensions/app_market

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_1433_npc-session-project](jiuwenswarm/extensions/app_market/npc-session-project.md) | B | 微缩生活岛 NPC 会话归属错误 | kdgmpw | 无独立 D：根据明确的故障修复要求落实既有项目绑定接口，无独立设计。 |
| [B_20261105_0941_app-stop-graceful-and-page-close](jiuwenswarm/extensions/app_market/app-stop-graceful-and-page-close.md) | B | 应用市场停止强杀跳过应用清理与页面关闭无感知 | kdgmpw | 无独立 D：修复依据既有契约（app-builder SKILL.md 的"应用负责优雅退出和会话清理、市场只终止进程"与扩展 README 的运行描述），无独立 D；新增契约已固化为 specs/jiuwenswarm/extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md。 |
| [B_20261105_1130_stale-page-cache-port-reuse](jiuwenswarm/extensions/app_market/stale-page-cache-port-reuse.md) | B | 端口复用后启动新应用显示上一个应用的缓存页面 | kdgmpw | 无独立 D：依据页面容器契约（specs/jiuwenswarm/channels/desktop/S_01_app-market-browser-containers.md 与 specs/jiuwenswarm/extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md）；缓存约束已补入 S_02 不变量与应用开发技能，无独立 D。 |
| [B_20261105_1459_web-url-host-403](jiuwenswarm/extensions/app_market/web-url-host-403.md) | B | Web 应用入口 403：静态服务未放行应用专属 localhost 主机 | kdgmpw | 无独立 D：依据 S_02_app-market-stop-and-page-lifecycle 的 Web 入口与来源隔离契约；本次修复使实现重新满足既有契约，无独立 D。 |
| [B_20261119_1416_builtin-extension-discovery](jiuwenswarm/extensions/app_market/B_20261119_1416_builtin-extension-discovery.md) | B | 本地默认启动未加载 Swarm 内置扩展导致 app_market RPC 不可用 | kdgmpw | 无独立 D：本次为本地启动装配缺陷修复，依据现有应用市场扩展契约与 Gateway 扩展加载实现，无独立设计。 |
| [B_20261124_1429_local-app-removal-and-conversion](jiuwenswarm/extensions/app_market/B_20261124_1429_local-app-removal-and-conversion.md) | B | 应用市场本地清理和 Hub 转换的一致性 | kdgmpw | 无独立 D：本次修正应用市场登记、目录归属与转换行为，沿用 S_03 契约和现有项目接口，无独立设计。 |

<a id="module-jiuwenswarm-extensions-runtime_management_extension"></a>

## jiuwenswarm/extensions/runtime_management_extension

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261124_1704_skills-uninstall-missing-name-400](extensions/B_20261124_1704_skills-uninstall-missing-name-400.md) | B | 企业版 Skill 卸载缺 name/null 返回 HTTP 500 改为 400 | meizi6 | 无独立 D：无独立 D；依据现有北向 POST /skills/actions/uninstall 与南向 DELETE /skills/{name} 契约，将路径占位符缺失归类为客户端入参错误。 |

<a id="module-jiuwenswarm-observability"></a>

## jiuwenswarm/observability

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261124_1602_issue-4327-observability](observability/B_20261124_1602_issue-4327-observability.md) | B | issue 4327：统一可观测性配置并分离轨迹存储与导出 | hh0205 | 无独立 D：依据 GitCode jiuwenswarm issue 4327，统一 observability 开关并分离轨迹存储、演进采集和可观测性导出的职责，无独立 D 文档。 |

<a id="module-jiuwenswarm-resources"></a>

## jiuwenswarm/resources

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261124_1602_issue-4327-observability](observability/B_20261124_1602_issue-4327-observability.md) | B | issue 4327：统一可观测性配置并分离轨迹存储与导出 | hh0205 | 无独立 D：依据 GitCode jiuwenswarm issue 4327，统一 observability 开关并分离轨迹存储、演进采集和可观测性导出的职责，无独立 D 文档。 |
<a id="module-jiuwenswarm-extensions-runtime_management_extension"></a>

## jiuwenswarm/extensions/runtime_management_extension

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261124_1704_skills-uninstall-missing-name-400](extensions/B_20261124_1704_skills-uninstall-missing-name-400.md) | B | 企业版 Skill 卸载缺 name/null 返回 HTTP 500 改为 400 | meizi6 | 无独立 D：无独立 D；依据现有北向 POST /skills/actions/uninstall 与南向 DELETE /skills/{name} 契约，将路径占位符缺失归类为客户端入参错误。 |

<a id="module-jiuwenswarm-resources-agent-workspace-skills"></a>

## jiuwenswarm/resources/agent/workspace/skills

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [B_20261104_1509_catalog-script-paths](jiuwenswarm/resources/B_20261104_1509_catalog-script-paths.md) | B | 内置能力搬迁后的生成目录与 PPT 同步路径回归 | kdgmpw | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |

<a id="module-jiuwenswarm-resources-agent-workspace-skills-workswarm-builtin"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_1433_npc-session-project](jiuwenswarm/extensions/app_market/npc-session-project.md) | B | 微缩生活岛 NPC 会话归属错误 | kdgmpw | 无独立 D：根据明确的故障修复要求落实既有项目绑定接口，无独立设计。 |

<a id="module-jiuwenswarm-resources-agent-workspace-skills-workswarm-builtin-references-capabilities-advanced-daily-report"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/advanced-daily-report

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261119_1547_daily-report-todo-collection](jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/advanced-daily-report/B_20261119_1547_daily-report-todo-collection.md) | B | 日报技能的待办采集始终为空 | liujiayu75 | 无独立 D：采集路径与现行存储契约不一致导致的实现缺陷；按 workspace todo.json 契约修复，无独立设计。 |

<a id="module-jiuwenswarm-resources-agent-workspace-skills-workswarm-builtin-references-capabilities-workswarm-app-builder"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261105_1459_web-url-host-403](jiuwenswarm/extensions/app_market/web-url-host-403.md) | B | Web 应用入口 403：静态服务未放行应用专属 localhost 主机 | kdgmpw | 无独立 D：依据 S_02_app-market-stop-and-page-lifecycle 的 Web 入口与来源隔离契约；本次修复使实现重新满足既有契约，无独立 D。 |

<a id="module-jiuwenswarm-runtime"></a>

## jiuwenswarm/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261120_0927_unknown-answer-source-hang](jiuwenswarm/runtime/B_20261120_0927_unknown-answer-source-hang.md) | B | 未知 source 的 answers 请求在团队会话中排队死锁、无终止帧 | openjiuwen_ai | 无独立 D：仅收紧既有 chat.send answers 校验的适用条件，不改变交互恢复协议、source 白名单与会话 lane 语义；无独立设计。 |
| [B_20261121_1545_team-pause-teardown-admission-race](jiuwenswarm/B_20261121_1545_team-pause-teardown-admission-race.md) | B | 团队暂停双执行与收尾窗口新消息误投递：回合交接竞态 | zhanglongmin | 无独立 D：依据运行日志时间线与代码逐跳核实定位，无独立设计文档；修复范围限定回合交接的暂停单入口、统一收尾屏障与路由守卫，不改变 Core 侧收尾顺序。 |
| [B_20261121_1817_parked-team-busy-snapshot-reseed](jiuwenswarm/B_20261121_1817_parked-team-busy-snapshot-reseed.md) | B | 暂停后页面持续转圈：挂起团队忙闲快照读到内存名册残留 | zhanglongmin | 无独立 D：依据运行日志与逐跳代码核实定位，无独立设计文档；修复走 Core 既有公开契约，不修改 agent-core。 |

<a id="module-jiuwenswarm-server"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261029_0927_missing-supervisor-entry](jiuwenswarm/server/B_20261029_0927_missing-supervisor-entry.md) | B | 重启 AgentServer 时缺少 supervisor 启动函数 | xinyang_bali | 无独立 D：用户请求重启时发现当前工作区入口不完整，恢复既有 Front/Worker 启动契约。 |
| [B_20261031_1042_pr9317-review](jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |
| [B_20261116_1540_interrupt-intent-contract](jiuwenswarm/server/B_20261116_1540_interrupt-intent-contract.md) | B | chat.interrupt 未知 intent 静默假成功：WS/HTTP 双入口统一契约校验 | ZHANGPP_ | 无独立 D：依据联调现场与 PR 检视意见定位，无独立设计文档；修复限定 intent 契约校验范围，并补回归测试。 |
| [B_20261125_1412_personal-edition-enterprise-team-methods](jiuwenswarm/server/B_20261125_1412_personal-edition-enterprise-team-methods.md) | B | 个人版收到企业 team 控制面方法时落入聊天分支：服务端入口补 edition 闸门 | mingo007 | 无独立 D：依据代码逐跳核实定位，无独立设计文档；修复只在服务端入口补一次 edition 判定，不改企业 handler 与协议枚举。 |
| [B_20261125_1658_unhandled-known-method-no-chat-fallback](jiuwenswarm/server/B_20261125_1658_unhandled-known-method-no-chat-fallback.md) | B | 枚举内未接 handler 的方法落入聊天分支：runtime 两个入口补通用兜底 | mingo007 | 无独立 D：静态审计全部枚举成员的认领层，确认只有 4 个聊天入口方法允许落入 chat 执行器；兜底不维护 175 方法白名单，而是挂在两个入口的收口点上。 |
| [B_20261126_0212_team-delete-partial-quota-release](jiuwenswarm/server/B_20261126_0212_team-delete-partial-quota-release.md) | B | 团队部分删除后配额占用残留：按会话清理与实体保留 | hanxinyang | 无独立 D：针对 Issue |

<a id="module-jiuwenswarm-server-control"></a>

## jiuwenswarm/server/control

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261112_1716_affinity-status-read](jiuwenswarm/server/control/B_20261112_1716_affinity-status-read.md) | B | 控制面遗漏亲和状态导致开关与说明不一致 | MengHan18 | 无独立 D：修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。 |
| [B_20261112_2025_permission-profile-refresh](jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |

<a id="module-jiuwenswarm-server-control-store"></a>

## jiuwenswarm/server/control/store

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261125_0115_cron-pin-sidebar-blackhole](jiuwenswarm/server/runtime/session/B_20261125_0115_cron-pin-sidebar-blackhole.md) | B | 缺 cron_id 的 cron 执行会话置顶后从侧边栏所有区域消失且无法取消置顶 | CharlesWang2030 | 无独立 D：由缺陷分析归档 JW-78 逆向定位，无独立设计文档；方案取舍（入口拒绝 vs 准入放宽 vs 元数据自愈）记录在本文档决策节。 |

<a id="module-jiuwenswarm-server-ipc"></a>

## jiuwenswarm/server/ipc

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261110_1042_issue6191-ipc-recovery](jiuwenswarm/server/ipc/B_20261110_1042_issue6191-ipc-recovery.md) | B | IPC 突发积压与降级就绪重连导致 Runtime 失败 | 刘瑜 | 无独立 D：对现有有界 IPC 和 Runtime 恢复状态机的小范围修复，无独立设计。 |

<a id="module-jiuwenswarm-server-personal_context"></a>

## jiuwenswarm/server/personal_context

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261124_1943_personal-context-failure-detail](jiuwenswarm/server/B_20261124_1943_personal-context-failure-detail.md) | B | 个人知识空间采集失败只显示「采集失败」，无法定位失败阶段与失败文件 | openjiuwen_ai | 无独立 D：只修采集失败诊断信息的透出与展示，不改变采集调度、存储契约和授权流程；无独立设计。 |

<a id="module-jiuwenswarm-server-runtime"></a>

## jiuwenswarm/server/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261110_1849_group-lock-cancellation](jiuwenswarm/server/runtime/B_20261110_1849_group-lock-cancellation.md) | B | 群任务取消期间等待文件锁清理完成 | ShowHsiang | 无独立 D：修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。 |
| [B_20261116_1701_my-plugin-installed-only](jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |

<a id="module-jiuwenswarm-server-runtime-agent_adapter"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_0946_ttse-codecheck-boundaries](jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1042_pr9317-review](jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |
| [B_20261031_1053_atomgit-review](jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |
| [B_20261106_1132_organization-round-cleanup](jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |
| [B_20261119_2249_deep-adapter-sync-build](jiuwenswarm/server/runtime/agent_adapter/B_20261119_2249_deep-adapter-sync-build.md) | B | DeepAdapter 冷启动同步构建阻塞事件循环 | yangzequ | 无独立 D：本次是对现有 DeepAdapter 创建链路的局部阻塞修复，无独立设计文档。 |
| [B_20261121_1545_team-pause-teardown-admission-race](jiuwenswarm/B_20261121_1545_team-pause-teardown-admission-race.md) | B | 团队暂停双执行与收尾窗口新消息误投递：回合交接竞态 | zhanglongmin | 无独立 D：依据运行日志时间线与代码逐跳核实定位，无独立设计文档；修复范围限定回合交接的暂停单入口、统一收尾屏障与路由守卫，不改变 Core 侧收尾顺序。 |
| [B_20261124_1717_main-agent-template-trace](jiuwenswarm/server/runtime/agent_adapter/B_20261124_1717_main-agent-template-trace.md) | B | 企业版 Work、Code 的主 Agent Trace 显示默认名 | 垚垚 | 无独立 D：本次修复企业版 Work、Code 主 Agent 的 Trace 展示名，沿用现有会话配置与观测接口，无独立设计文档。 |
| [B_20261126_1045_ui-lang-over-yaml-zh](jiuwenswarm/enterprise/config/B_20261126_1045_ui-lang-over-yaml-zh.md) | B | 企业版 UI 语言被 AgentServer yaml preferred_language zh 盖过 | whds5279 | 无独立 D：无独立 D；依据 issue |
| [B_20261126_1052_team-memory-rail-race](jiuwenswarm/server/runtime/agent_adapter/B_20261126_1052_team-memory-rail-race.md) | B | Team 订阅与用户请求并发卸载记忆 Rail | Wang Tao | 无独立 D：按已确认的最小修复方案恢复被动订阅和记忆生命周期契约，无独立新设计。 |
| [B_20261126_1625_enterprise-cron-permission-rail-a4p-decouple](jiuwenswarm/agents/harness/common/rails/B_20261126_1625_enterprise-cron-permission-rail-a4p-decouple.md) | B | 企业版 Cron 权限护栏与 A4P 解耦，ask 无人值守 fail-closed | zhangxiangyu52 | 无独立 D：无独立 D：依据企业版安全护栏与定时任务无人值守契约的既有产品要求及 PR |

<a id="module-jiuwenswarm-server-runtime-marketplace"></a>

## jiuwenswarm/server/runtime/marketplace

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261116_2059_hub-package-root](jiuwenswarm/server/runtime/B_20261116_2059_hub-package-root.md) | B | Hub 安装把包内子资产 manifest 误判为多个包根 | openjiuwen_ai | 无独立 D：仅修正包根识别的深度判定，不改变 Hub 包格式、安装状态机与 marketplace 契约；无独立设计。 |

<a id="module-jiuwenswarm-server-runtime-session"></a>

## jiuwenswarm/server/runtime/session

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261125_0115_cron-pin-sidebar-blackhole](jiuwenswarm/server/runtime/session/B_20261125_0115_cron-pin-sidebar-blackhole.md) | B | 缺 cron_id 的 cron 执行会话置顶后从侧边栏所有区域消失且无法取消置顶 | CharlesWang2030 | 无独立 D：由缺陷分析归档 JW-78 逆向定位，无独立设计文档；方案取舍（入口拒绝 vs 准入放宽 vs 元数据自愈）记录在本文档决策节。 |
| [B_20261126_0954_native-npc-team-first-turn](jiuwenswarm/server/runtime/session/B_20261126_0954_native-npc-team-first-turn.md) | B | 原生 NPC 队伍首轮发言失败 | kdgmpw | 无独立 D：修复现有原生 NPC Team 装配契约。 |
| [B_20261126_1308_session-metadata-corrupted-marker](jiuwenswarm/server/runtime/session/B_20261126_1308_session-metadata-corrupted-marker.md) | B | 损坏 metadata.json 的会话在列表中伪装成合法旧会话，团队身份字段静默丢失 | zhangpu | 无独立 D：防御性呈现契约修复，复用 _read_metadata 既有读路径补显式失败状态并在合成层打标、投影层透传，无独立设计文档。 |
| [B_20261126_1628_upload-doc-block-auto-title](jiuwenswarm/B_20261126_1628_upload-doc-block-auto-title.md) | B | 会话自动标题被【上传文档】协议块污染 | whds5279 | 无独立 D：修复既有标题生成与上传协议块注入的跨层契约冲突，不引入新方案。 |

<a id="module-jiuwenswarm-server-runtime-skill"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_0936_skill-action-param-type-validation](jiuwenswarm/server/runtime/skill/B_20261103_0936_skill-action-param-type-validation.md) | B | 技能安装/更新/卸载入口参数类型校验缺失修复 | openjiuwen_ai | 无独立 D：三个入口的参数契约（string 标识、boolean force）已由前端 webHttpClient 与 EnterpriseSkillSourcePanel 既有调用形态声明，本次为入口 类型校验层缺失的补接，无独立设计。 |
| [B_20261103_1636_builtin-catalog-upgrade](jiuwenswarm/server/runtime/skill/builtin-catalog-upgrade.md) | B | 旧内置技能未归一与应用市场入口不明显 | kdgmpw | [D-20261103-1123-builtin-skill-catalog](../designs/active/D-20261103-1123-builtin-skill-catalog/design.md) |
| [B_20261116_1412_skill-import-validation-and-list](jiuwenswarm/server/runtime/skill/B_20261116_1412_skill-import-validation-and-list.md) | B | 技能导入元数据与 ZIP 路径安全校验修复 | zhangzhongteng | 无独立 D：用户确认统一个人版技能导入和市场安装的元数据校验，并修复 ZIP 原始文件名及路径检查；无独立设计。 |
| [B_20261117_1019_skill-upload-temp-leak](jiuwenswarm/server/runtime/skill/B_20261117_1019_skill-upload-temp-leak.md) | B | 技能导入的两阶段上传临时目录永久残留 | mengliang0116 | 无独立 D：两阶段上传缺少文件生命周期归属，属实现缺陷的小范围修复，不引入新的架构设计；接口与生命周期契约记录在本文。 |

<a id="module-jiuwenswarm-server-supervisor"></a>

## jiuwenswarm/server/supervisor

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261107_1505_6226-forwarding-replay](jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261110_1042_issue6191-ipc-recovery](jiuwenswarm/server/ipc/B_20261110_1042_issue6191-ipc-recovery.md) | B | IPC 突发积压与降级就绪重连导致 Runtime 失败 | 刘瑜 | 无独立 D：对现有有界 IPC 和 Runtime 恢复状态机的小范围修复，无独立设计。 |

<a id="module-jiuwenswarm-server-worker"></a>

## jiuwenswarm/server/worker

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261105_1518_worker-dispatch-slot-accounting](jiuwenswarm/server/worker/B_20261105_1518_worker-dispatch-slot-accounting.md) | B | Worker 派发容量排除已返回的 handler | kdgmpw | 无独立 D：按会话中确认的最小修复方案调整 Worker 内部计数。 |

<a id="module-scripts"></a>

## scripts

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [B_20261118_1150_memory-frozen-registration](scripts/B_20261118_1150_memory-frozen-registration.md) | B | 修复冻结后端中 JiuwenMemory 动态实现漏包 | ToNy_St4rk | 无独立 D：按已确认的发布包故障进行局部打包修复，不改变 Memory 的公共接口或运行时配置契约。 |
| [B_20261125_2150_windows-installer-restore-bootstrap](scripts/B_20261125_2150_windows-installer-restore-bootstrap.md) | B | Windows 安装器恢复进程隔离初始化与分派测试修正 | XinchenZzz | 无独立 D：对 Issue 7056 已有恢复事务的局部故障修复及测试适配，没有新增独立设计。 |

<a id="module-tests-fixtures-ttse_plugin"></a>

## tests/fixtures/ttse_plugin

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-tests-unit_tests-agentserver"></a>

## tests/unit_tests/agentserver

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [B_20261031_1048_ttse-core-dependent-ut](tests/unit_tests/agentserver/B_20261031_1048_ttse-core-dependent-ut.md) | B | 暂时移除依赖未合入 core 工厂能力的插件 UT | xinyang_bali | 无独立 D：用户明确要求暂时删除依赖 core 的相关 UT，不调整依赖或运行实现，无独立 D。 |
| [B_20261116_1701_my-plugin-installed-only](jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |
| [B_20261117_1050_adapter-ut-fixtures](tests/unit_tests/agentserver/B_20261117_1050_adapter-ut-fixtures.md) | B | 图片降级提示与付费搜索 UT 的测试替身和断言漂移 | TwZhang | 无独立 D：根据用户提供的 UT 报告修正测试替身及副作用断言，无独立设计。 |

<a id="module-tests-unit_tests-gateway"></a>

## tests/unit_tests/gateway

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-tests-unit_tests-personal_context"></a>

## tests/unit_tests/personal_context

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-tests-unit_tests-server"></a>

## tests/unit_tests/server

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261110_1849_group-lock-cancellation](jiuwenswarm/server/runtime/B_20261110_1849_group-lock-cancellation.md) | B | 群任务取消期间等待文件锁清理完成 | ShowHsiang | 无独立 D：修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。 |

<a id="module-tests-unit_tests-server-extensions"></a>

## tests/unit_tests/server/extensions

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-unclassified"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261026_1602_code-multimodal-loading](jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | 待核实 |
| [B_20261026_1602_code-multimodal-loading](jiuwenswarm/server/runtime/agent_adapter/code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | 待核实 |
