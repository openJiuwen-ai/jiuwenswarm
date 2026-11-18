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
| [agent_gateway/channel_manager/im_platforms/xiaoyi](#module-agent_gateway-channel_manager-im_platforms-xiaoyi) |
| [agent_gateway/message_handler](#module-agent_gateway-message_handler) |
| [gateway/channel_manager/web](#module-gateway-channel_manager-web) |
| [jiuwenswarm](#module-jiuwenswarm) |
| [jiuwenswarm/agents/harness/code/rails/heartbeat](#module-jiuwenswarm-agents-harness-code-rails-heartbeat) |
| [jiuwenswarm/agents/harness/common/rsi](#module-jiuwenswarm-agents-harness-common-rsi) |
| [jiuwenswarm/agents/harness/common/tools/cron](#module-jiuwenswarm-agents-harness-common-tools-cron) |
| [jiuwenswarm/agents/harness/team](#module-jiuwenswarm-agents-harness-team) |
| [jiuwenswarm/agents/swarm](#module-jiuwenswarm-agents-swarm) |
| [jiuwenswarm/channels/tui](#module-jiuwenswarm-channels-tui) |
| [jiuwenswarm/channels/web](#module-jiuwenswarm-channels-web) |
| [jiuwenswarm/channels/web/frontend](#module-jiuwenswarm-channels-web-frontend) |
| [jiuwenswarm/channels/web/frontend/src/features/rsi](#module-jiuwenswarm-channels-web-frontend-src-features-rsi) |
| [jiuwenswarm/channels/web/frontend/src/features/settings](#module-jiuwenswarm-channels-web-frontend-src-features-settings) |
| [jiuwenswarm/channels/web/frontend/src/features/settings/modules/models](#module-jiuwenswarm-channels-web-frontend-src-features-settings-modules-models) |
| [jiuwenswarm/common](#module-jiuwenswarm-common) |
| [jiuwenswarm/server](#module-jiuwenswarm-server) |
| [jiuwenswarm/server/control](#module-jiuwenswarm-server-control) |
| [jiuwenswarm/server/ipc](#module-jiuwenswarm-server-ipc) |
| [jiuwenswarm/server/runtime](#module-jiuwenswarm-server-runtime) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-jiuwenswarm-server-runtime-agent_adapter) |
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

<a id="module-jiuwenswarm-agents-harness-code-rails-heartbeat"></a>

## jiuwenswarm/agents/harness/code/rails/heartbeat

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-jiuwenswarm-agents-harness-common-rsi"></a>

## jiuwenswarm/agents/harness/common/rsi

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261114_1302_terminal-metrics-and-model-errors](jiuwenswarm/agents/harness/common/rsi/B_20261114_1302_terminal-metrics-and-model-errors.md) | B | RSI 终态统计、侧栏运行态和论文模型错误修复 | l_x_d | 无独立 D：按用户确认修复现有行为及 Issue 6558、6539、6532，无独立设计。 |

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

<a id="module-jiuwenswarm-agents-swarm"></a>

## jiuwenswarm/agents/swarm

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261106_1132_organization-round-cleanup](jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |

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

<a id="module-jiuwenswarm-channels-web-frontend"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261030_1250_org-snapshot-removes-mirrored-workers](jiuwenswarm/channels/web/frontend/B_20261030_1250_org-snapshot-removes-mirrored-workers.md) | B | 组织 UI 快照投影整表覆盖导致镜像 workflow worker 从成员面板消失 | xiajing | 无独立 D：对既有"快照完整替换"契约（跨 Team 切换不串数据）的成员级例外修复，属 F_20261027_1026 特性线的验证期 bugfix，无独立设计文档。 |
| [B_20261103_1112_raw-file-range-seek](jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261107_1505_6226-forwarding-replay](jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261112_1405_stop-btn-stuck-after-skip](jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |
| [B_20261113_1416_cron-drawer-double-submit](jiuwenswarm/channels/web/frontend/B_20261113_1416_cron-drawer-double-submit.md) | B | 定时任务抽屉确认按钮无提交中防护，慢响应窗口内双击创建重复任务 | gunner_beat | 无独立 D：交互层缺陷修复，复用同仓库 HeartbeatPanel 既有 submitting 机制，无独立设计文档。社区 issue 与修复 PR 为事实源：GitCode issue |
| [B_20261116_1701_my-plugin-installed-only](jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |

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

<a id="module-jiuwenswarm-common"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261112_1716_affinity-status-read](jiuwenswarm/server/control/B_20261112_1716_affinity-status-read.md) | B | 控制面遗漏亲和状态导致开关与说明不一致 | MengHan18 | 无独立 D：修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。 |
| [B_20261112_2025_permission-profile-refresh](jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |

<a id="module-jiuwenswarm-server"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261029_0927_missing-supervisor-entry](jiuwenswarm/server/B_20261029_0927_missing-supervisor-entry.md) | B | 重启 AgentServer 时缺少 supervisor 启动函数 | xinyang_bali | 无独立 D：用户请求重启时发现当前工作区入口不完整，恢复既有 Front/Worker 启动契约。 |
| [B_20261031_1042_pr9317-review](jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |

<a id="module-jiuwenswarm-server-control"></a>

## jiuwenswarm/server/control

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261112_1716_affinity-status-read](jiuwenswarm/server/control/B_20261112_1716_affinity-status-read.md) | B | 控制面遗漏亲和状态导致开关与说明不一致 | MengHan18 | 无独立 D：修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。 |
| [B_20261112_2025_permission-profile-refresh](jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |

<a id="module-jiuwenswarm-server-ipc"></a>

## jiuwenswarm/server/ipc

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261110_1042_issue6191-ipc-recovery](jiuwenswarm/server/ipc/B_20261110_1042_issue6191-ipc-recovery.md) | B | IPC 突发积压与降级就绪重连导致 Runtime 失败 | 刘瑜 | 无独立 D：对现有有界 IPC 和 Runtime 恢复状态机的小范围修复，无独立设计。 |

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

<a id="module-jiuwenswarm-server-runtime-skill"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 关联 D |
|---|---|---|---|---|
| [B_20261103_0936_skill-action-param-type-validation](jiuwenswarm/server/runtime/skill/B_20261103_0936_skill-action-param-type-validation.md) | B | 技能安装/更新/卸载入口参数类型校验缺失修复 | openjiuwen_ai | 无独立 D：三个入口的参数契约（string 标识、boolean force）已由前端 webHttpClient 与 EnterpriseSkillSourcePanel 既有调用形态声明，本次为入口 类型校验层缺失的补接，无独立设计。 |
| [B_20261116_1412_skill-import-validation-and-list](jiuwenswarm/server/runtime/skill/B_20261116_1412_skill-import-validation-and-list.md) | B | 技能导入元数据与 ZIP 路径安全校验修复 | zhangzhongteng | 无独立 D：用户确认统一个人版技能导入和市场安装的元数据校验，并修复 ZIP 原始文件名及路径检查；无独立设计。 |

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
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | 待核实 |
