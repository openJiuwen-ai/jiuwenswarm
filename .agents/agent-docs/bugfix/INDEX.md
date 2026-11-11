---
title: 故障修复 B索引
author: ShowHsiang
author_email: xiangxiaoneng@h-partners.com
generated: true
---

# 故障修复 B索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

按模块分组；跨模块文档在各相关分组展示。

## 模块导航

| 模块 |
|---|
| [agent_gateway/channel_manager/im_platforms/xiaoyi](#module-1) |
| [agent_gateway/message_handler](#module-2) |
| [gateway/channel_manager/web](#module-3) |
| [jiuwenswarm](#module-4) |
| [jiuwenswarm/agents/harness/code/rails/heartbeat](#module-5) |
| [jiuwenswarm/agents/harness/common/tools/cron](#module-6) |
| [jiuwenswarm/agents/swarm](#module-7) |
| [jiuwenswarm/channels/tui](#module-8) |
| [jiuwenswarm/channels/web](#module-9) |
| [jiuwenswarm/channels/web/frontend](#module-10) |
| [jiuwenswarm/server](#module-11) |
| [jiuwenswarm/server/runtime](#module-12) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-13) |
| [jiuwenswarm/server/runtime/skill](#module-14) |
| [jiuwenswarm/server/supervisor](#module-15) |
| [jiuwenswarm/server/worker](#module-16) |
| [scripts](#module-17) |
| [tests/fixtures/ttse_plugin](#module-18) |
| [tests/unit_tests/agentserver](#module-19) |
| [tests/unit_tests/gateway](#module-20) |
| [tests/unit_tests/personal_context](#module-21) |
| [tests/unit_tests/server](#module-22) |
| [tests/unit_tests/server/extensions](#module-23) |
| [未分类](#module-24) |

<a id="module-1"></a>

## agent_gateway/channel_manager/im_platforms/xiaoyi

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | agent_gateway/channel_manager/im_platforms/xiaoyi, agent_gateway/message_handler | agent_gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, agent_gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-2"></a>

## agent_gateway/message_handler

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | agent_gateway/channel_manager/im_platforms/xiaoyi, agent_gateway/message_handler | agent_gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, agent_gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-3"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261102_1644_cron-web-default-project-id](gateway/channel_manager/B_20261102_1644_cron-web-default-project-id.md) | B | 前端未选项目创建定时任务落库 project_id 回退为空串 | kdgmpw | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronProjectDisplay.ts, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：恢复 4b00671f6（#5713）已实现的行为，属于对该修复在企业版合入后被回退的局部回归修复，无独立设计。 |

<a id="module-4"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261030_2020_pause-refires-consumed-popups](jiuwenswarm/server/runtime/agent_adapter/B_20261030_2020_pause-refires-consumed-popups.md) | B | 暂停/取消后已消费的 human 弹窗被重放层重投 | xiajing | jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts | 无独立 D：重放层过滤由主干既有机制承载，本修复补触发层清场与消费层去重两段。 |
| [B_20261031_0946_ttse-codecheck-boundaries](jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | jiuwenswarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/openjiuwen_fs_rwlock_patch.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, scripts/package_ttse_plugin.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1053_atomgit-review](jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/agentserver/test_ttse_background_drain.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |

<a id="module-5"></a>

## jiuwenswarm/agents/harness/code/rails/heartbeat

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/agents/harness/code/rails/heartbeat | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-6"></a>

## jiuwenswarm/agents/harness/common/tools/cron

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/agents/harness/code/rails/heartbeat | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-7"></a>

## jiuwenswarm/agents/swarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261106_1132_organization-round-cleanup](jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | jiuwenswarm/agents/swarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/swarm/group_execution.py, jiuwenswarm/agents/swarm/group_org_bootstrap.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/agents/swarm/test_group_org_bootstrap.py | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |

<a id="module-8"></a>

## jiuwenswarm/channels/tui

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1451_hub-default-url-consistency](jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/tui | jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/teamskills.ts, jiuwenswarm/channels/web/frontend/tests/skillHubEndpointContract.test.mjs, docs/en/SwarmSkills.md, docs/zh/SwarmSkills.md, docs/en/Configuration.md, docs/zh/配置信息.md | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-9"></a>

## jiuwenswarm/channels/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1146_chat-cold-load](jiuwenswarm/channels/web/B_20261031_1146_chat-cold-load.md) | B | 会话嵌套路由冷加载白屏修复 | kdgmpw | jiuwenswarm/channels/web | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/channels/web/test_web_static_cache_headers.py | 无独立 D：对已有静态服务契约的局部修复，与 nginx 的资源别名保持兼容，无独立设计。 |
| [B_20261102_1104_cron-stable-model-selection](jiuwenswarm/channels/web/B_20261102_1104_cron-stable-model-selection.md) | B | 定时任务模型选择丢失稳定 ID 与执行失败报错不可见修复 | kdgmpw | jiuwenswarm/channels/web | jiuwenswarm/channels/web/frontend/src/types/cron.ts, jiuwenswarm/channels/web/frontend/src/components/CronPanel/CronTaskDrawer.tsx, jiuwenswarm/channels/web/frontend/src/components/CronPanel/index.tsx, jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronModelSelection.ts, jiuwenswarm/channels/web/frontend/src/utils/wsEventDedup.ts, jiuwenswarm/channels/web/frontend/src/features/free-models/chatError.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/package.json, jiuwenswarm/channels/web/frontend/tests/cronModelSelection.test.mjs, jiuwenswarm/channels/web/frontend/tests/modelPicker.test.mjs, jiuwenswarm/channels/web/frontend/tests/wsEventDedup.test.mjs, jiuwenswarm/channels/web/frontend/tests/chatError.test.mjs | 无独立 D：稳定选择契约（ModelPicker selection/onSelectionChange、CronJob.model_selection、 chat.error 去重窗口）均为既有实现，本次是 cron 抽屉这一旧契约调用方的迁移补接与 去重键缺维修复，无独立设计。 |
| [B_20261103_1112_raw-file-range-seek](jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/test_app_web_raw_file.py | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261103_1451_hub-default-url-consistency](jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/tui | jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/teamskills.ts, jiuwenswarm/channels/web/frontend/tests/skillHubEndpointContract.test.mjs, docs/en/SwarmSkills.md, docs/zh/SwarmSkills.md, docs/en/Configuration.md, docs/zh/配置信息.md | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-10"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261030_1250_org-snapshot-removes-mirrored-workers](jiuwenswarm/channels/web/frontend/B_20261030_1250_org-snapshot-removes-mirrored-workers.md) | B | 组织 UI 快照投影整表覆盖导致镜像 workflow worker 从成员面板消失 | xiajing | jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/frontend/src/stores/teamSliceApply.ts, jiuwenswarm/channels/web/frontend/src/stores/teamSelectorStore.ts, jiuwenswarm/channels/web/frontend/tests/teamSliceApply.test.mjs | 无独立 D：对既有"快照完整替换"契约（跨 Team 切换不串数据）的成员级例外修复，属 F_20261027_1026 特性线的验证期 bugfix，无独立设计文档。 |
| [B_20261103_1112_raw-file-range-seek](jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/test_app_web_raw_file.py | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261107_1505_6226-forwarding-replay](jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | jiuwenswarm/server/supervisor, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, tests/unit_tests/agentserver/test_supervisor_forwarding.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |

<a id="module-11"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_0927_missing-supervisor-entry](jiuwenswarm/server/B_20261029_0927_missing-supervisor-entry.md) | B | 重启 AgentServer 时缺少 supervisor 启动函数 | xinyang_bali | jiuwenswarm/server | jiuwenswarm/server/app_agentserver.py | 无独立 D：用户请求重启时发现当前工作区入口不完整，恢复既有 Front/Worker 启动契约。 |
| [B_20261031_1042_pr9317-review](jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | jiuwenswarm/server, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/server/app_agentserver.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/test_app_agentserver.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |

<a id="module-12"></a>

## jiuwenswarm/server/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261110_1849_group-lock-cancellation](jiuwenswarm/server/runtime/B_20261110_1849_group-lock-cancellation.md) | B | 群任务取消期间等待文件锁清理完成 | ShowHsiang | jiuwenswarm/server/runtime, tests/unit_tests/server | jiuwenswarm/server/runtime/group_service.py, tests/unit_tests/server/test_group_lock.py, tests/conftest.py | 无独立 D：修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。 |

<a id="module-13"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_0946_ttse-codecheck-boundaries](jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | jiuwenswarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/openjiuwen_fs_rwlock_patch.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, scripts/package_ttse_plugin.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1042_pr9317-review](jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | jiuwenswarm/server, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/server/app_agentserver.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/test_app_agentserver.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |
| [B_20261031_1053_atomgit-review](jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/agentserver/test_ttse_background_drain.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |
| [B_20261106_1132_organization-round-cleanup](jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | jiuwenswarm/agents/swarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/swarm/group_execution.py, jiuwenswarm/agents/swarm/group_org_bootstrap.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/agents/swarm/test_group_org_bootstrap.py | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |

<a id="module-14"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_0936_skill-action-param-type-validation](jiuwenswarm/server/runtime/skill/B_20261103_0936_skill-action-param-type-validation.md) | B | 技能安装/更新/卸载入口参数类型校验缺失修复 | openjiuwen_ai | jiuwenswarm/server/runtime/skill | jiuwenswarm/server/runtime/skill/skill_manager.py, tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py | 无独立 D：三个入口的参数契约（string 标识、boolean force）已由前端 webHttpClient 与 EnterpriseSkillSourcePanel 既有调用形态声明，本次为入口 类型校验层缺失的补接，无独立设计。 |

<a id="module-15"></a>

## jiuwenswarm/server/supervisor

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261107_1505_6226-forwarding-replay](jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | jiuwenswarm/server/supervisor, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, tests/unit_tests/agentserver/test_supervisor_forwarding.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |

<a id="module-16"></a>

## jiuwenswarm/server/worker

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261105_1518_worker-dispatch-slot-accounting](jiuwenswarm/server/worker/B_20261105_1518_worker-dispatch-slot-accounting.md) | B | Worker 派发容量排除已返回的 handler | kdgmpw | jiuwenswarm/server/worker | jiuwenswarm/server/worker/event_store.py | 无独立 D：按会话中确认的最小修复方案调整 Worker 内部计数。 |

<a id="module-17"></a>

## scripts

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-18"></a>

## tests/fixtures/ttse_plugin

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-19"></a>

## tests/unit_tests/agentserver

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [B_20261031_1048_ttse-core-dependent-ut](tests/unit_tests/agentserver/B_20261031_1048_ttse-core-dependent-ut.md) | B | 暂时移除依赖未合入 core 工厂能力的插件 UT | xinyang_bali | tests/unit_tests/agentserver | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：用户明确要求暂时删除依赖 core 的相关 UT，不调整依赖或运行实现，无独立 D。 |

<a id="module-20"></a>

## tests/unit_tests/gateway

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | tests/unit_tests/gateway, tests/unit_tests/personal_context | tests/unit_tests/gateway/test_cron_live_bus.py, tests/unit_tests/personal_context/test_closed_class_inventory.py | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-21"></a>

## tests/unit_tests/personal_context

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | tests/unit_tests/gateway, tests/unit_tests/personal_context | tests/unit_tests/gateway/test_cron_live_bus.py, tests/unit_tests/personal_context/test_closed_class_inventory.py | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-22"></a>

## tests/unit_tests/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261110_1849_group-lock-cancellation](jiuwenswarm/server/runtime/B_20261110_1849_group-lock-cancellation.md) | B | 群任务取消期间等待文件锁清理完成 | ShowHsiang | jiuwenswarm/server/runtime, tests/unit_tests/server | jiuwenswarm/server/runtime/group_service.py, tests/unit_tests/server/test_group_lock.py, tests/conftest.py | 无独立 D：修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。 |

<a id="module-23"></a>

## tests/unit_tests/server/extensions

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-24"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261026_1602_code-multimodal-loading](jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | — | — | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | — | — | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | — | — | 待核实 |
