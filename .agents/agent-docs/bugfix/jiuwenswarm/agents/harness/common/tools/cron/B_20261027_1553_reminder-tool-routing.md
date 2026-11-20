---
id: B_20261027_1553_reminder-tool-routing
title: Code 模式中文提醒错误路由到 heartbeat
type: B
author: MengHan18
author_email: hanmeng22@huawei.com
modules:
  - jiuwenswarm/agents/harness/common/tools/cron
  - jiuwenswarm/agents/harness/code/rails/heartbeat
files:
  - jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py
  - tests/unit_tests/agentserver/test_tool_discovery_language.py
designs: []
design_note: 针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。
---

# Code 模式中文提醒错误路由到 heartbeat

## 现象与影响

Issue [#5615](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5615) 中，用户在 Code 模式要求“2分钟后提醒我喝水”，模型却调用会话绑定的 `heartbeat_create_job`，独立 cron 列表没有新增任务。

定位基线为 upstream develop `761ec07cc`。用真实注册入口构造 cron、heartbeat 和 session 工具卡后，在英文工具集上查询中文提醒，修复前前六项没有 `cron_create_job`，可以复现错误选路的前置条件。

## 根因

Code 模式默认使用英文工具卡。当前渐进式检索采用 BM25 词法匹配，不会自动把中文查询翻译成英文；中文“提醒”与英文 cron 描述缺少共同词元，因此 `cron_create_job` 无法召回。与此同时，heartbeat 的短摘要没有明确排除独立提醒，模型容易把“稍后执行”误判成“回到当前会话继续任务”。

agent-core 已提供英文 `tool_search` 元数据并按运行语言装配，所以本问题不是缺少英文版 `tool_search`。本次也不修改 agent-core、检索算法或全局配置开关。

## 修复决策

1. 在集中发现目录中为 `cron_create_job` 维护英文摘要和中文能力别名，覆盖独立定时任务、一次性提醒和周期通知；别名只进入检索索引，不修改原始工具描述。
2. 在集中发现目录的 `heartbeat_create_job` 英文摘要中明确它只用于稍后继续当前会话中的既有任务，并把独立调度指向 `cron_create_job`；摘要避免重复提醒、通知等竞争关键词，不修改原始 heartbeat 工具定义。
3. 不改变工具名称、参数 schema、权限、存储或调度行为。受限会话没有 `cron_create_job` 时，描述补丁不会恢复被禁用的工具。

## 回归范围

新增回归使用真实 cron、heartbeat 和 session 工具构建入口，覆盖：

- 英文和中文运行语言下，中英文一次性提醒及周期通知均可召回 `cron_create_job`；
- Issue 中的中文提醒在英文工具集里将 `cron_create_job` 排在首位；
- 渐进式目录的 cron/heartbeat 摘要能表达职责边界；
- 显式搜索 heartbeat 仍可找到原工具；
- 创建权限关闭时不会发现 `cron_create_job`。

相关回归测试与本地验证均已通过。

## 防错模式

运行提示词语言与用户查询语言是两个独立维度。词法工具发现需要检查真实注册卡片与用户表达是否存在可匹配词元，也要保证短摘要在截断预算内说明相邻工具的职责边界。
