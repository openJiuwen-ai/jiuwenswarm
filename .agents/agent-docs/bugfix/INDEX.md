---
title: 故障修复 B索引
author: alan_cheng
author_email: chengshuo5@huawei.com
generated: true
---

# 故障修复 B索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 4 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [jiuwenswarm/agents/harness/common/prompt](#module-1) | 1 |
| [jiuwenswarm/channels/web/frontend](#module-2) | 1 |
| [jiuwenswarm/server/runtime/agent_adapter](#module-3) | 1 |
| [未分类](#module-4) | 3 |

<a id="module-1"></a>

## jiuwenswarm/agents/harness/common/prompt

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1110_steer-undelivered-answer](jiuwenswarm/server/runtime/agent_adapter/B_20261027_1110_steer-undelivered-answer.md) | B | steer 后原问题答案未交付 | 刘瑜 | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/runtime/agent_adapter/session_input.py, jiuwenswarm/agents/harness/common/prompt/priority_registry.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs, tests/unit_tests/agentserver/test_session_input_delivery.py, tests/integration/test_session_steering_sdk.py, tests/integration/test_steer_image_paths_sdk.py | 无独立 D：在 Issue 4516 当前 PR 的人工验收中确认，对现有 steer 回答交付语义进行局部修复，无独立设计文档。 |

<a id="module-2"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1110_steer-undelivered-answer](jiuwenswarm/server/runtime/agent_adapter/B_20261027_1110_steer-undelivered-answer.md) | B | steer 后原问题答案未交付 | 刘瑜 | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/runtime/agent_adapter/session_input.py, jiuwenswarm/agents/harness/common/prompt/priority_registry.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs, tests/unit_tests/agentserver/test_session_input_delivery.py, tests/integration/test_session_steering_sdk.py, tests/integration/test_steer_image_paths_sdk.py | 无独立 D：在 Issue 4516 当前 PR 的人工验收中确认，对现有 steer 回答交付语义进行局部修复，无独立设计文档。 |

<a id="module-3"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1110_steer-undelivered-answer](jiuwenswarm/server/runtime/agent_adapter/B_20261027_1110_steer-undelivered-answer.md) | B | steer 后原问题答案未交付 | 刘瑜 | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/runtime/agent_adapter/session_input.py, jiuwenswarm/agents/harness/common/prompt/priority_registry.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs, tests/unit_tests/agentserver/test_session_input_delivery.py, tests/integration/test_session_steering_sdk.py, tests/integration/test_steer_image_paths_sdk.py | 无独立 D：在 Issue 4516 当前 PR 的人工验收中确认，对现有 steer 回答交付语义进行局部修复，无独立设计文档。 |

<a id="module-4"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261026_1602_code-multimodal-loading](jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | — | — | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | — | — | 待核实 |
| [B_20261026_2035_gateway-contract-collection](tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | — | — | 待核实 |
