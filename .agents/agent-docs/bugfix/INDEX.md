---
title: 故障修复 B索引
author: 刘瑜
author_email: liuyu524@huawei.com
generated: true
---

# 故障修复 B索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 5 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [gateway/channel_manager/im_platforms/xiaoyi](#module-1) | 1 |
| [gateway/message_handler](#module-2) | 1 |
| [jiuwenswarm/channels/desktop](#module-3) | 1 |
| [jiuwenswarm/server/supervisor](#module-4) | 1 |
| [jiuwenswarm/server/worker](#module-5) | 1 |
| [未分类](#module-6) | 3 |

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

## jiuwenswarm/channels/desktop

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_1910_worker-lifecycle-recovery](jiuwenswarm/server/supervisor/B_20261029_1910_worker-lifecycle-recovery.md) | B | 修复常驻 Worker 退出、历史端点兼容和耗尽重试后的恢复 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py, jiuwenswarm/server/supervisor/identity.py, jiuwenswarm/debug_launcher.py, jiuwenswarm/channels/desktop/electron/main.cjs, scripts/jiuwenswarm.spec | 无独立 D：修复已确认的常驻与最终退出契约及回退前暴露的兼容回归，未建立独立方案文档。 |

<a id="module-4"></a>

## jiuwenswarm/server/supervisor

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_1910_worker-lifecycle-recovery](jiuwenswarm/server/supervisor/B_20261029_1910_worker-lifecycle-recovery.md) | B | 修复常驻 Worker 退出、历史端点兼容和耗尽重试后的恢复 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py, jiuwenswarm/server/supervisor/identity.py, jiuwenswarm/debug_launcher.py, jiuwenswarm/channels/desktop/electron/main.cjs, scripts/jiuwenswarm.spec | 无独立 D：修复已确认的常驻与最终退出契约及回退前暴露的兼容回归，未建立独立方案文档。 |

<a id="module-5"></a>

## jiuwenswarm/server/worker

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_1910_worker-lifecycle-recovery](jiuwenswarm/server/supervisor/B_20261029_1910_worker-lifecycle-recovery.md) | B | 修复常驻 Worker 退出、历史端点兼容和耗尽重试后的恢复 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py, jiuwenswarm/server/supervisor/identity.py, jiuwenswarm/debug_launcher.py, jiuwenswarm/channels/desktop/electron/main.cjs, scripts/jiuwenswarm.spec | 无独立 D：修复已确认的常驻与最终退出契约及回退前暴露的兼容回归，未建立独立方案文档。 |

<a id="module-6"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261026_1602_code-multimodal-loading](jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | — | — | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | — | — | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | — | — | 待核实 |
