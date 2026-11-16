---
title: D/F/B/S 文档总索引
author: nypdmax
author_email: chengkunck@gmail.com
generated: true
---

# D/F/B/S 文档总索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

按模块分组；跨模块文档在各相关分组展示。

## 模块导航

| 模块 |
|---|
| [jiuwenswarm/agents/harness/common/tools](#module-1) |
| [jiuwenswarm/common](#module-2) |
| [jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi](#module-3) |
| [jiuwenswarm/gateway/message_handler](#module-4) |
| [jiuwenswarm/server/runtime/gateway_adapter](#module-5) |

<a id="module-1"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_authorized-file-download](specs/jiuwenswarm/common/S_01_authorized-file-download.md) | S | 授权文件下载规约 | nypdmax | jiuwenswarm/common, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/server/runtime/gateway_adapter | jiuwenswarm/common/verified_download_assets.py, jiuwenswarm/common/file_download_token.py, jiuwenswarm/agents/harness/common/tools/send_file_to_user.py, jiuwenswarm/server/runtime/gateway_adapter/workspace_file_adapter.py | — |

<a id="module-2"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_authorized-file-download](specs/jiuwenswarm/common/S_01_authorized-file-download.md) | S | 授权文件下载规约 | nypdmax | jiuwenswarm/common, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/server/runtime/gateway_adapter | jiuwenswarm/common/verified_download_assets.py, jiuwenswarm/common/file_download_token.py, jiuwenswarm/agents/harness/common/tools/send_file_to_user.py, jiuwenswarm/server/runtime/gateway_adapter/workspace_file_adapter.py | — |

<a id="module-3"></a>

## jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](bugfix/gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi, jiuwenswarm/gateway/message_handler | jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, jiuwenswarm/gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-4"></a>

## jiuwenswarm/gateway/message_handler

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](bugfix/gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi, jiuwenswarm/gateway/message_handler | jiuwenswarm/gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, jiuwenswarm/gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-5"></a>

## jiuwenswarm/server/runtime/gateway_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_authorized-file-download](specs/jiuwenswarm/common/S_01_authorized-file-download.md) | S | 授权文件下载规约 | nypdmax | jiuwenswarm/common, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/server/runtime/gateway_adapter | jiuwenswarm/common/verified_download_assets.py, jiuwenswarm/common/file_download_token.py, jiuwenswarm/agents/harness/common/tools/send_file_to_user.py, jiuwenswarm/server/runtime/gateway_adapter/workspace_file_adapter.py | — |
