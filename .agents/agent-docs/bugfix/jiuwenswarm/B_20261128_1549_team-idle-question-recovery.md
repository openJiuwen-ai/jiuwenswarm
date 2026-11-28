---
id: B_20261128_1549_team-idle-question-recovery
title: Team 空闲后回答澄清问题仍残留处理中状态
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/runtime/session
  - jiuwenswarm/server/worker
files:
  - jiuwenswarm/runtime/session/coordinator.py
  - tests/unit_tests/runtime/test_runtime_session_coordinator.py
  - tests/unit_tests/agentserver/test_team_completion_runtime_snapshot.py
designs: []
design_note: 日常缺陷修复，依据当前 Session 类型、Team 生命周期和恢复接口测试；未新建独立设计。
---

# Team 空闲后回答澄清问题仍残留处理中状态

关联 [Issue #6926](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6926)。

## 现象与根因

Team 提出问题后进入 idle，持久响应拥有者被停放到 `WAITING_FOR_INPUT`，但仍持有待回答的
`waiting_control_ids`。`_control_parent()` 只接受 `RUNNING` 和 `WAITING_FOR_CONTROL`，
使随后到达的 `TEAM_CONTROL` 找不到原问题拥有者。Team 的交互投递仍能成功，Coordinator 却漏掉
`_answered()` 清理；主流最终结束时又因残留问题 ID 进入 `WAITING_FOR_CONTROL`，Worker 恢复快照继续报忙。

附件记录了先空闲、再回答、业务完成、恢复接口仍返回 `processing` 的顺序。现场缺少完整执行快照和
exe 构建 SHA；源码受控复现证明上述机制，不将其视为原始 exe 全流程回放。

本记录与 [Team 内存名册残留导致恢复误报忙](B_20261121_1817_parked-team-busy-snapshot-reseed.md)
属于不同根因：本文定位 Coordinator 的待答问题残留，原记录的成员名册同步结论继续有效。

## 修复决策与边界

在已有 session、generation、取消状态和问题 ID 过滤后，允许持久 Team 拥有者在
`WAITING_FOR_INPUT` 下被匹配。成功投递后沿 `_answered()` 清除该问题 ID；投递失败继续保留问题，
不改变父子执行优先级、E2A 格式或公开接口。

未采用强制把整个会话置为 idle：同会话可能已经有新工作。未关闭观察订阅：其连接寿命不代表业务执行。
未把所有 `WAITING_FOR_INPUT` 执行统一视为控制目标：新增条件限定为持久 Team，且必须匹配具体问题。

当前集中 specs 未提供该模块独立规约。历史 `SDD-0029` 仍为 draft，只作为背景导航；本修复维护当前
“按会话代次和问题 ID 回答原问题、成功才清理”的已有行为，不将历史草案写成已批准方案。

## 回归证据

- 两个目标分支先运行新增用例：各 `3 failed, 1 passed`，失败分别覆盖暂停后的控制查找和恢复接口误报忙。
- Coordinator 用例覆盖成功回答、投递失败保留问题、错误问题 ID、其他会话。
- Worker 组合用例覆盖先 idle 与不经过 idle 的对照，实际使用 Coordinator、Team 生命周期适配器、
  E2A 编码和恢复快照；完成后断言无待答问题、恢复空闲，观察订阅仍连接。
- 相关完整定向回归结果在提交前补齐。Python 3.11，两个分支分别使用各自 `uv.lock` 的冻结依赖；
  Core、Gateway、E2A 的安装提交与锁文件逐项核对。

模型输出与 Team 事件采用可控替身，未执行现场 exe、浏览器或真实模型回放。
候选代码、协议依赖或 Team 轮次状态推进改变时，应重验上述问答与恢复路径。
