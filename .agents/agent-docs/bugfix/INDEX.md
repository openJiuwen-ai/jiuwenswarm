---
title: 故障修复 B索引
author: xinyang_bali
author_email: 835397263@qq.com
generated: true
---

# 故障修复 B索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 11 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [gateway/channel_manager/im_platforms/xiaoyi](#module-1) | 1 |
| [gateway/message_handler](#module-2) | 1 |
| [jiuwenswarm](#module-3) | 2 |
| [jiuwenswarm/server](#module-4) | 2 |
| [jiuwenswarm/server/runtime/agent_adapter](#module-5) | 3 |
| [scripts](#module-6) | 1 |
| [tests/fixtures/ttse_plugin](#module-7) | 1 |
| [tests/unit_tests/agentserver](#module-8) | 2 |
| [tests/unit_tests/gateway](#module-9) | 1 |
| [tests/unit_tests/personal_context](#module-10) | 1 |
| [tests/unit_tests/server/extensions](#module-11) | 1 |
| [未分类](#module-12) | 3 |

<a id="module-1"></a>

## gateway/channel_manager/im_platforms/xiaoyi

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | gateway/channel_manager/im_platforms/xiaoyi, gateway/message_handler | gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-2"></a>

## gateway/message_handler

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | gateway/channel_manager/im_platforms/xiaoyi, gateway/message_handler | gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-3"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_0946_ttse-codecheck-boundaries](jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | jiuwenswarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/openjiuwen_fs_rwlock_patch.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, scripts/package_ttse_plugin.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1053_atomgit-review](jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/agentserver/test_ttse_background_drain.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |

<a id="module-4"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_0927_missing-supervisor-entry](jiuwenswarm/server/B_20261029_0927_missing-supervisor-entry.md) | B | 重启 AgentServer 时缺少 supervisor 启动函数 | xinyang_bali | jiuwenswarm/server | jiuwenswarm/server/app_agentserver.py | 无独立 D：用户请求重启时发现当前工作区入口不完整，恢复既有 Front/Worker 启动契约。 |
| [B_20261031_1042_pr9317-review](jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | jiuwenswarm/server, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/server/app_agentserver.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/test_app_agentserver.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |

<a id="module-5"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_0946_ttse-codecheck-boundaries](jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | jiuwenswarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/openjiuwen_fs_rwlock_patch.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, scripts/package_ttse_plugin.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1042_pr9317-review](jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | jiuwenswarm/server, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/server/app_agentserver.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/test_app_agentserver.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |
| [B_20261031_1053_atomgit-review](jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/agentserver/test_ttse_background_drain.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |

<a id="module-6"></a>

## scripts

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-7"></a>

## tests/fixtures/ttse_plugin

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-8"></a>

## tests/unit_tests/agentserver

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [B_20261031_1048_ttse-core-dependent-ut](tests/unit_tests/agentserver/B_20261031_1048_ttse-core-dependent-ut.md) | B | 暂时移除依赖未合入 core 工厂能力的插件 UT | xinyang_bali | tests/unit_tests/agentserver | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：用户明确要求暂时删除依赖 core 的相关 UT，不调整依赖或运行实现，无独立 D。 |

<a id="module-9"></a>

## tests/unit_tests/gateway

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | tests/unit_tests/gateway, tests/unit_tests/personal_context | tests/unit_tests/gateway/test_cron_live_bus.py, tests/unit_tests/personal_context/test_closed_class_inventory.py | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-10"></a>

## tests/unit_tests/personal_context

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | tests/unit_tests/gateway, tests/unit_tests/personal_context | tests/unit_tests/gateway/test_cron_live_bus.py, tests/unit_tests/personal_context/test_closed_class_inventory.py | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-11"></a>

## tests/unit_tests/server/extensions

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-12"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261026_1602_code-multimodal-loading](jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | — | — | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | — | — | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | — | — | 待核实 |
