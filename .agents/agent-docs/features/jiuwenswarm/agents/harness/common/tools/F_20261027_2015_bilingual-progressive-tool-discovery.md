---
id: F_20261027_2015_bilingual-progressive-tool-discovery
title: 中英文渐进式工具发现
type: F
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
- tests/unit_tests/agentserver/test_tool_discovery_language.py
designs: []
design_note: 针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号
---

# 中英文渐进式工具发现实施归档

## 关联依据

- 日期：2026-10-27。
- 关联问题：JiuwenSwarm Issue #5615 的跨工具扩展目标。
- 当前契约：`S_01_tool-discovery-language.md`。
- core 依赖：`agent-core` 的渐进式发现元数据扩展。

## 实际实现与开发决策

JiuwenSwarm 集中维护内置工具的英文目录摘要和中文搜索别名。默认检索通过
`ToolCard.properties["progressive_discovery"]` 把元数据交给 core；优化检索直接把同一份隐藏文本
传给 BM25。搜索结果仍返回原始工具 schema，MCP 工具保留提供方原文。

没有采用“强制模型先把 query 翻译成英文”作为正确性前提，因为翻译会增加一次不稳定的模型决策，
并且容易损失工具名、路径、错误码和中英混合专有词。翻译可以作为未来的补充召回手段，不能取代
确定性的别名索引。

## 实际执行与证据

相关 core 与 JiuwenSwarm 回归测试通过，本地验证成功。

## 验证结论

Code 默认英文和 Work 英文模式均可用中文 query 检索已登记的内置工具；默认与优化路径使用同一份
发现数据，原始 schema、权限指纹和 MCP 原文契约保持不变。JiuwenSwarm 已固定到包含发现元数据能力的
agent-core 提交 `d6a815488d2e1242f8a4d860cb5f96b8a5a5a906`。

## 已知遗留

新内置工具需要同时补充发现目录。分类内工具由完整性测试约束；不属于分类、且由 rail 直接注册的
工具仍需要在评审时确认是否应加入目录。

## 2026-10-27 检视后修订

本次续写完善同一发现元数据方案，保留此前实现和验证记录。检视发现英文 `display_en` 会替换
原始 heartbeat 目录摘要，省略此前已明确的“只继续既有任务，独立调度使用 cron”边界；新增
摘要恢复这项约束，且避免重复提醒、通知等竞争关键词。cron 的别名补充“通知我”“定时通知”
和“一次性提醒”，覆盖只有通知表达的自然语言请求。

测试增加真实产品注册入口：Adapter 注册 cron 和 session 工具，HeartbeatRail 在 DeepAgent
初始化时注册心跳工具；同样通过 Adapter 装配默认和优化检索。回归观察实际注册的 tool_search
输出、模型调用前回调过滤后的工具数组，以及 core 系统提示词或优化模式的分类列表。
用例同时检查创建/查询/取消提醒、继续既有任务、读取其他会话，及 cron 重新注册/删除后
目录与索引的实时行为。完整描述与参数仍对照当前注册卡片，避免把别名混入执行契约。

英文查询 `Schedule a daily notification` 在 Work 中文工具卡集合中仍将 heartbeat 排在 cron
之前，cron 位于默认五个候选的预算内；该场景按候选召回验证，并依赖目录与完整工具描述
区分最终操作，不宣称 BM25 已保证任意跨语言表达的首位准确率。保留此限制，不为单条
查询重复堆叠搜索词或改变检索算法。中文提醒/通知、查询/取消任务和会话操作的选定用例
则要求目标排在首位。

相关注册链路、双语检索和提醒路由回归通过，本地验证成功。
