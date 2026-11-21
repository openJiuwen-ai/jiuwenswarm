---
id: B_20261121_2236_team-db-lock-retry
title: 团队成员执行状态更新遇到 SQLite 锁错误
type: B
author: GeirvaldrOden
author_email: qzqanlhy2012@163.com
modules:
  - jiuwenswarm
  - jiuwenswarm/server/worker
  - tests/unit_tests
  - tests/unit_tests/agentserver
files:
  - jiuwenswarm/openjiuwen_team_db_lock_patch.py
  - jiuwenswarm/server/worker/runtime_backend.py
  - tests/unit_tests/test_openjiuwen_team_db_lock_patch.py
  - tests/unit_tests/agentserver/test_agentserver_regressions.py
designs: []
design_note: 针对既有 Team Member DAO 写入的短暂 SQLite 锁冲突做局部兼容修复，无独立设计文档。
---

# 团队成员执行状态更新遇到 SQLite 锁错误

## 现象与影响

团队成员执行过程中，更新 `team_member.execution_status` 偶发抛出
`sqlite3.OperationalError: database is locked`，导致本轮任务以错误结束。截图中的 SQL 为
成员状态更新语句；本修复只覆盖这一 DAO 写入遇到可重试瞬时错误的路径。

## 关联设计与当前契约

没有独立设计。继续使用 OpenJiuwen `MemberDao.update_member_execution_status` 的既有接口和
`retry_on_transient` 重试策略，不改变状态值、DAO 参数、数据库结构或其他持久化错误语义。

## 根因与修复决策

并发写入 SQLite 时，成员执行状态更新可能遇到暂时持有的数据库写锁；该 DAO 调用原先没有经过
SDK 的瞬时错误重试 helper，因此锁释放后也不会再次执行状态更新。

在 Worker 运行时入口安装幂等兼容包装，只委托 SDK `retry_on_transient` 重试该成员状态写入。
如果 SDK 方法本身已包含锁重试则不重复包装；SDK/接口不兼容时发出明确警告。没有对所有数据库错误
做宽泛重试，也没有修改数据库事务或成员执行流程。

## 回归证据与适用范围

- 新增 `test_execution_status_write_retries_sqlite_lock`：首次 DAO 调用抛出锁错误，重试成功后断言
  返回值及两次调用次数。
- 扩展 Worker runtime patch 顺序测试，确保团队数据库锁补丁在运行时入口安装。
- Conda `jiuwenswarm` 环境中执行
  `python -m pytest -o addopts='' tests/unit_tests/test_openjiuwen_team_db_lock_patch.py -q`：
  **1 passed**。测试使用该环境的真实 OpenJiuwen `MemberDao` 与重试 helper，确认锁错误后第二次调用成功。
- 标准库替身检查通过：锁错误后重试成功、重复安装不叠加包装、测试重置能恢复方法。
- Worker runtime patch 顺序测试未能执行：该环境已安装的 `agent_gateway.extensions.sdk.skill_source`
  缺少源码期望的 `SKILL_CAPABILITIES`，在 `agentserver/conftest.py` 导入阶段失败；需要同步匹配的
  `agent_gateway` 依赖后再重验启动接入。

## 防错模式与资料同步

团队 DAO 的 SQLite 瞬时锁错误应复用 SDK 的瞬时错误策略并限定在可安全重试的写入边界；
不把任意 `OperationalError` 当作锁错误。本次未改变持久化契约，无需新增 spec 或全局规则。

## 遗留与后续

Worker 接入级定向 pytest 仍待重验，需先同步匹配的 `agent_gateway` 依赖；DAO 锁重试单测已在 Conda
环境通过。若 SDK DAO 或重试 helper 接口变化，应检查并更新该兼容补丁。
