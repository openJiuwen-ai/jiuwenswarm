---
id: B_20261126_0212_team-delete-partial-quota-release
title: 团队部分删除后配额占用残留：按会话清理与实体保留
type: B
author: hanxinyang
author_email: xyhan226@163.com
modules:
  - jiuwenswarm/server
  - jiuwenswarm/enterprise/team
files:
  - jiuwenswarm/server/handlers/team.py
  - jiuwenswarm/enterprise/team/session_cleanup.py
  - jiuwenswarm/enterprise/team/quota.py
  - tests/unit_tests/server/test_server_regressions.py
  - tests/unit_tests/agentserver/test_team_quota.py
designs: []
design_note: 针对 Issue #141 的删除清理时机修复，无独立设计。
---

# 团队部分删除后配额占用残留：按会话清理与实体保留

## 现象与影响

Issue [#141](https://gitcode.com/ZYQ5333/jiuwenclaw/issues/141)：删除团队后管理面显示"0 个团队、已用 1"，配额被一条不存在的团队永久占住。

`team_quota_lease` 的 `committed` 行把 `expires_at` 置为 `NULL`，不会自然过期；`team_entity` 已删除但租约仍在时，用量统计就会出现这条"幽灵占用"。用户额度被占满后无法再创建团队。

## 根因

`handle_team_delete` 只在 `result.deleted` 为真时释放配额。而 runtime 的部分删除路径（`session_provisioner._delete_team_locked` 的 `if failed:` 分支）会返回 `deleted=False`、`session_ids=已成功删除的会话`、`failed_session_ids=失败会话`，并刻意保留 team entity 和 route binding 给幸存会话。于是这些已删除会话的 `team_create` 租约没人回收。

修复不能简单地"对所有 `session_ids` 调用原来的 purge"：`purge_team_for_deleted_session` 是按 `team_id` 级联的，会删除 team entity、该团队的全部租约（含幸存成员的 Y 租约）和全部 route binding。部分删除时幸存会话仍在用这些记录，级联清理会把它们变成孤儿——比原来的配额残留更严重。

## 修复决策

1. `handle_team_delete` 继续对所有 `result.session_ids` 逐条清理，单条失败隔离，不影响其他会话和响应。
2. 给 `purge_team_for_deleted_session` 和 `TeamQuotaService.drop_occupancy` 增加默认 `False` 的 `session_only` 参数。部分删除传 `True`：只删该会话自己的 `team_create` 租约、按 session 删 route binding、`unbind_session`；不动 entity、不按 `team_id` 级联。全量删除保持原有 team 级清理。
3. `team_name` 兜底只用于全量删除且没有 `session_ids` 的场景；部分删除时幸存会话可能仍持有该名字。
4. 删掉用空字符串 `session_id` 占位的写法，改为显式的 `(session_id, team_name)` 任务列表，失败日志同时输出两者，便于区分按会话与按名字两条路径。

MySQL 与 SQLite 两条实现同步收紧：`_purge_mysql` 在 `session_only` 下跳过 entity/成员/按 `team_id` 的租约和 binding 删除，只保留按 `key`、按 `team_session_id` 的删除与本地解绑。

## 回归范围

- `tests/unit_tests/server/test_server_regressions.py`：部分删除只清理成功会话并传 `session_only=True`；单条失败不影响其他会话；零删除不清理；全量删除按名字兜底。
- `tests/unit_tests/agentserver/test_team_quota.py`：注入真实 `SqliteTeamControlStore`、`TeamQuotaService`、`InMemoryTeamRouteBindingStore`，构造主会话已删、成员会话幸存的多成员团队，验证 `session_only=True` 后 entity、成员关系、幸存成员 Y 租约和 route binding 全部保留，只移除被删会话的 X 租约。

## 防错模式

"部分成功"的清理必须按实际成功的粒度执行。一个按父实体级联的清理函数，不能直接用于子集合的部分删除；需要在接口上显式区分清理范围，而不是靠调用方猜。
