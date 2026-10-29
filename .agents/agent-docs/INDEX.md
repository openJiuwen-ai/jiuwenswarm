---
title: D/F/B/S 文档总索引
author: 刘瑜
author_email: liuyu524@huawei.com
generated: true
---

# D/F/B/S 文档总索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 3 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [jiuwenswarm/channels/desktop](#module-1) | 2 |
| [jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi](#module-2) | 1 |
| [jiuwenswarm/gateway/message_handler](#module-3) | 1 |
| [jiuwenswarm/server/supervisor](#module-4) | 2 |
| [jiuwenswarm/server/worker](#module-5) | 2 |

<a id="module-1"></a>

## jiuwenswarm/channels/desktop

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_1910_worker-lifecycle-recovery](bugfix/jiuwenswarm/server/supervisor/B_20261029_1910_worker-lifecycle-recovery.md) | B | 修复常驻 Worker 退出、历史端点兼容和耗尽重试后的恢复 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py, jiuwenswarm/server/supervisor/identity.py, jiuwenswarm/debug_launcher.py, jiuwenswarm/channels/desktop/electron/main.cjs, scripts/jiuwenswarm.spec | 无独立 D：修复已确认的常驻与最终退出契约及回退前暴露的兼容回归，未建立独立方案文档。 |
| [S_01_worker-lifecycle](specs/jiuwenswarm/server/supervisor/S_01_worker-lifecycle.md) | S | Runtime Worker 生命周期与恢复规约 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py | — |

<a id="module-2"></a>

## jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](bugfix/gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi, jiuwenswarm/gateway/message_handler | jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, jiuwenswarm/gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-3"></a>

## jiuwenswarm/gateway/message_handler

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](bugfix/gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi, jiuwenswarm/gateway/message_handler | jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, jiuwenswarm/gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-4"></a>

## jiuwenswarm/server/supervisor

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_1910_worker-lifecycle-recovery](bugfix/jiuwenswarm/server/supervisor/B_20261029_1910_worker-lifecycle-recovery.md) | B | 修复常驻 Worker 退出、历史端点兼容和耗尽重试后的恢复 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py, jiuwenswarm/server/supervisor/identity.py, jiuwenswarm/debug_launcher.py, jiuwenswarm/channels/desktop/electron/main.cjs, scripts/jiuwenswarm.spec | 无独立 D：修复已确认的常驻与最终退出契约及回退前暴露的兼容回归，未建立独立方案文档。 |
| [S_01_worker-lifecycle](specs/jiuwenswarm/server/supervisor/S_01_worker-lifecycle.md) | S | Runtime Worker 生命周期与恢复规约 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py | — |

<a id="module-5"></a>

## jiuwenswarm/server/worker

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_1910_worker-lifecycle-recovery](bugfix/jiuwenswarm/server/supervisor/B_20261029_1910_worker-lifecycle-recovery.md) | B | 修复常驻 Worker 退出、历史端点兼容和耗尽重试后的恢复 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py, jiuwenswarm/server/supervisor/identity.py, jiuwenswarm/debug_launcher.py, jiuwenswarm/channels/desktop/electron/main.cjs, scripts/jiuwenswarm.spec | 无独立 D：修复已确认的常驻与最终退出契约及回退前暴露的兼容回归，未建立独立方案文档。 |
| [S_01_worker-lifecycle](specs/jiuwenswarm/server/supervisor/S_01_worker-lifecycle.md) | S | Runtime Worker 生命周期与恢复规约 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py | — |
