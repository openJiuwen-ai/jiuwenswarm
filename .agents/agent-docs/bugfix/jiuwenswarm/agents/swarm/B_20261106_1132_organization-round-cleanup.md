---
id: B_20261106_1132_organization-round-cleanup
title: 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复
type: B
author: "“ZhangRan-Zora"
author_email: "“hidadamao@gmail.com”"
designs: []
design_note: 按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。
modules:
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/agents/swarm/group_execution.py
  - jiuwenswarm/agents/swarm/group_org_bootstrap.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - tests/agents/swarm/test_group_org_bootstrap.py
---

## 问题与证据

基线 ca09cf5dd，PR 9636 的三条意见均可在隔离场景中复现：存在待回答问题时 after_invoke 先结束组织回合，计划可能提前失败；工具失败释放 acceptance 后，历史 run attempt 永久保护计划，所有回合结束后卡片仍 running；初次建卡保存后、publish 返回前取消，调用方的计划 ID 尚未赋值且未进入后续取消保护区，遗留 pending 计划和 running 卡片。

## 修复决定与契约

待回答问题检查前移至结束组织回合之前，等待用户时保留 open turn 和绑定。真正结束回合时清除该成员的历史尝试标记；其他成员的历史尝试不再阻止本成员释放未接受绑定，仍由 open turn 保护尚未结束的回合。acceptance、实际运行、队列和前序阻塞保护保留。失败调用仍可在活跃回合中重试。

记录函数在本地处理 CancelledError，使用已经记录的 plan ID 调度未启动计划清理并重新抛出取消；清理任务以集合持有强引用，结束后移除。复用原有未启动判断，不关闭已经接受、执行中、等待回答或排队的计划。继续由代理调用 run_team 拉起实例，不修改单专家、单专家团及混合 @ 路由。

## 回归与检视

0.2.9 当前工作区、锁定依赖、临时数据目录，pytest 参数为 -o addopts='' -o log_cli=false -o asyncio_mode=auto。原 13 个群聊/组织/Summary/server 测试文件新增 8 个参数化场景，最终 381 passed（7.76s）。测试覆盖等待回答及回答后收尾、两种成员结束顺序、实际卡片 upsert 的保存后发布取消，以及 accepted/open/queued 保护。保留原有失败后重试、普通群聊工具与问答测试。

既有组合测试的旧计划仍处于 pending，会阻塞后续计划；修正该测试时保留这一前提，不将它错误断言为可立即失败。独立新增测试验证无阻塞情况下所有失败回合结束后确实关闭。

中英文 SwarmChatGroupOrganization 文档同步上述契约。按 jw-review 核对生产调用方、清理幂等性和已启动计划保护；未运行真实模型任务，无端到端时延结论。macOS Worker 修复不在此次提交范围。
