---
id: S_01_tool-discovery-language
title: 渐进式工具发现语言与原始执行契约
type: S
author: MengHan18
author_email: hanmeng22@huawei.com
modules:
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/agents/harness/common/rails
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py
  - jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py
  - jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py
  - jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_code.py
---

# 渐进式工具发现语言与原始执行契约

最近一次修订：2026-10-27。

## 范围与语言选择

Adapter 通过 `_resolve_runtime_language()` 为会话自己的工具目录选择显示语言。普通 Code 默认 `en`；Work 与现有团队语言覆盖保持原解析规则。不得依据用户本轮消息是否包含中文修改运行语言，不改变面向用户的回复语言。

`progressive_tool_optimize` 保持原开关语义：优化模式使用分类目录的 list/get_schema；未启用优化时仍使用框架自带 query 接口。不能假定修改分类目录就覆盖了默认 query 路径。

## 发现目录与查询

- `tool_discovery_catalog.py` 是产品内置工具发现信息的唯一来源。静态分类成员必须具有英文摘要与中文能力别名；缺项由测试失败暴露，不得从工具名自动生成伪翻译。
- 内置工具的 BM25 文本由原名称、原描述、原参数、英文摘要和中文别名组成。中文或英文 query 均可检索，别名只用于发现，不进入调用 schema。
- 默认路径把同一份数据写入 `ToolCard.properties["progressive_discovery"]`，由 agent-core `ProgressiveToolRail` 读取；优化路径由 `ToolRetrievalToolkit` 直接读取。两个开关路径不得分别维护翻译。
- 目录摘要必须保留相邻工具的选路边界。`heartbeat_create_job` 的英文摘要在最终渲染后仍须说明只用于继续当前会话的既有任务，并将独立调度指向 `cron_create_job`；摘要不得堆叠提醒、通知等竞争关键词，也不能只在原始工具描述中保留边界。
- `cron_create_job` 的中文别名覆盖提醒和通知表达，包括“提醒我”“一次性提醒”“通知我”“定时通知”；这些词参与词法召回，不代表已具备语义检索能力。
- 分类模式按 category 或 keyword 发现工具，再按实际返回名称获取 schema。query 模式在识别能力后可使用当前目录已公开的准确名称，不得猜测未注册工具名称或重复相近检索。
- MCP 通过 `McpToolCard` / `McpToolInfo` 类型与内置元数据隔离，名称、描述和 schema 保留提供方原文；连接器分组仍遵守 MCP 注册名称协议。未知工具不进行自动翻译。

## 执行与版本不变量

`get_schema` 返回真实注册工具的完整描述和参数约束；发现摘要不是完整约束的替代品。已有对象参数传递提示继续生效，不翻译参数名、枚举值或用户数据。

仅当前 live deferred registry 中的工具可以被发现和调用。英文摘要、搜索别名、旧目录和历史结果均不能恢复已删除或失去授权的工具。

冻结快照和实时目录增量使用相同的摘要投影；schema 与授权版本仍依据原始工具的完整 fingerprint。发现元数据参与搜索索引版本，修改别名会重建索引，但不得制造权限变化。

缓存的 `ToolRetrievalToolkit` 仅在运行语言一致时复用。语言画像变化时创建新 toolkit，并沿用会话适配器的既有重建边界，不能在同一个冻结目录中混用两种语言。

## 回归入口与边界

`tests/unit_tests/agentserver/test_tool_discovery_language.py` 覆盖 Code/Work 英文运行语言、全部静态分类成员、默认/优化检索的中文 query、原始 schema 与 MCP 原文。既有 tool retrieval 与权限用例继续作为消费者回归。

注册链路回归通过真实 Adapter 的 cron/session 注册入口、HeartbeatRail 及 DeepAgent 初始化装配两种发现方案；通过实际工具查询与模型调用前回调检查排序、工具过滤和最终目录边界。覆盖 Code 英文、Work 中英文，以及 cron 重新注册和删除后实时搜索结果的变化。排序用例同时覆盖创建、查询、取消提醒、会话跟进和跨会话读取。

该契约不保证模型总能作出正确语义选择，不等于引入多语言语义搜索。原始故障与验证依据见 `B_20261027_1553_reminder-tool-routing`。
