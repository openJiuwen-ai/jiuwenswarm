---
id: B_20261128_1418_team-roster-departed-member-reappearance
title: 离队成员刷新后复活：名册出口未收敛到 _split_roster 与历史回放复活
type: B
author: DaiYuxin511
author_email: 455472400@qq.com
modules:
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/agents/harness/team/handlers/team_monitor_handler.py
  - jiuwenswarm/channels/web/frontend/src/features/teamHistoryPanelRestore.ts
  - jiuwenswarm/channels/web/frontend/tests/teamHistoryPanelRestore.test.mjs
  - tests/unit_tests/agentserver/test_monitor_handler.py
designs: []
design_note: 依据运行现象与逐跳代码核实定位，无独立设计文档；修复收敛到 agent-core 既有 is_departed_member / _split_roster 公开契约，不修改 agent-core。issue #181 实际归属 GitHub 镜像仓 openJiuwen-ai/jiuwenswarm，本仓内同名 #181 为无关文档 issue（见遗留 NH-11147-03）。
---

# 离队成员刷新后复活：名册出口未收敛到 _split_roster 与历史回放复活

## 现象与影响

成员 `shut_down` 离队后刷新页面，离队成员重新出现在团队花名册与任务面板上（issue
#181）。三条出口各复活一处：

- `/join` 成员校验名单：`get_member_list`（live）与 `get_member_list_from_db`（DB
  降级）未过滤 `shut_down`，离队成员仍在校验名单里，可被 roster announce / read
  fallback 重新画回活跃名册。
- 前端历史回放：`teamHistoryPanelRestore` 的成员列表完全来自历史事件回放，历史可
  复活 `shut_down` 成员，而快照已透传的 `departed_members`（DB 真相）从未被消费来
  修剪。
- 任务状态降级：刷新后 `in_progress` 任务可能被快照重取降级为 `cancelled` /
  `pending`（陈旧 / 部分快照或丢失的 `TASK_CANCELLED` 发布）。

## 根因

- **后端名册出口契约分裂**：`team.snapshot` 路径（`get_team_snapshot` /
  `get_team_snapshot_from_db`）经 `_split_roster` 用
  `is_departed_member(status == shut_down)` 把离队成员挪到 `departed_members`
  （`team_monitor_handler.py:153`），但两条成员名单出口（`get_member_list` live、
  `get_member_list_from_db` DB 降级）未走同一判定，照原样返回全部成员（含
  `shut_down`）。`member_status.py` 的模块注释明确"后端对前端有多个名册出口，它们
  必须对'谁还在名册上'用同一个判断"，本次即该契约在两处出口漏网。
- **前端历史回放复活**：`reconcileTasksFromSnapshot` 只对账 `tasks`，成员列表完全
  由历史事件派生；`shut_down` 成员的离队事件若没翻到 / 缺失，历史回放就把它画回
  名册，而快照的 `departed_members`（DB 真相，`_split_roster` 输出）从未被消费来
  修剪。
- **任务状态降级无仲裁**：`mergeTeamSnapshotTaskWithHistory` 直接以快照 status 为
  准，陈旧 / 部分快照把 `in_progress` 降级为 `cancelled` / `pending` 时没有时间戳
  仲裁，刷新后活跃任务被误显为已取消。

## 修复决策

- **后端两出口收敛到 `is_departed_member`**：`get_member_list`（live，
  `monitor.get_members()` 后过滤）与 `get_member_list_from_db`（DB 行
  `get_team_members` 后过滤）均
  `[m for m in members if not is_departed_member(m.status)]`，与 `_split_roster`
  逐字一致；`dormant` / `shutdown_requested` 保留（dormant 成员被 @mention 唤醒，
  不是离队）。`/join` 校验名单、membership 名字映射与 roster announce / read
  fallback 因此一致：离队成员不再出现在活跃名册。
- **前端用快照 `departed_members` 修剪历史回放名册**：新增
  `pruneDepartedMembersFromSnapshot` 用快照 `departed_members`（DB 真相）的
  `member_id` 集合修剪历史派生的 `members`；空集合 no-op 返回原数组引用避免无谓
  新对象。`reconcileTasksFromSnapshot` 改名 `reconcileStateFromSnapshot`，在
  `snapshotTasks` 为空分支也执行成员修剪（原空快照直接 `return state` 不修剪）。
  `collectSnapshotMemberIds` 对非数组 / 非 record 项防御性跳过。
- **任务状态双向仲裁**：新增 `resolveSnapshotTaskStatus`，当 history 记录为
  active（`planning` / `in_progress` / `in_review`）且快照欲降级为 `cancelled` /
  `pending` 且 history `timestamp` 严格新于快照 `timestamp` 时保留 history 状态
  ——陈旧 / 部分快照或丢失的 `TASK_CANCELLED` 发布不再降级更新的 `in_progress`；
  非 active history / 无 history / 缺时间戳均保守直通快照状态（快照权威，DB 真相）。
  仲裁用合并前原始 `timestamp`（非 `Math.max` 合并值），stale 快照读不降级
  `in_progress` 的意图可实现。
- `TeamSnapshotResponse` 接口补 `departed_members` 字段声明供前端读取。

## 回归证据与适用范围

后端 `tests/unit_tests/agentserver/test_monitor_handler.py` 新增 2 用例：

- `test_get_member_list_drops_shut_down_keeps_dormant`（live 路径：drop
  `shut_down`，keep `dormant` / `shutdown_requested`）；
- `test_get_member_list_from_db_drops_shut_down_keeps_dormant`（DB 降级路径
  同语义）。

前端 `teamHistoryPanelRestore.test.mjs` 新增 5 用例：

- `pruneDepartedMembersFromSnapshot`：drop 确认离队 / 空集 no-op 返回原数组 /
  `dormant` 保留；
- `resolveSnapshotTaskStatus`：新 `in_progress` 压过陈旧 `cancelled` / 新快照
  降级可信 / 无 history 直通 / 非 active history 不仲裁。

检视报告记录双端定向测试全绿（后端 `test_monitor_handler.py` 54 passed、前端
`npm run test:team-history-panel` 18 passed，均含本 PR 新增用例）。

未验证范围：top-up 分支（`server/handlers/team.py:711-721`，live roster 空时用 DB
补 `members` 但未同步补 `departed_members`）属建议级缺口（NH-11147-01），未做
端到端复现；手动 ST 留终验。

## 防错模式与遗留

- **防错**：后端名册出口（`team.snapshot` / `get_member_list` /
  `get_member_list_from_db`）与前端历史回放修剪全部收敛到
  `is_departed_member(shut_down)` 单一判定；后续改 roster 离队语义时三处出口 +
  前端修剪同查，本记录即契约落点。
- **遗留**：
  - NH-11147-01：top-up 分支 `departed_members` 不从 DB 补充，边缘场景（live roster
    为空且 DB 有 `shut_down` 成员且 history 回放复活）前端修剪拿不到 DB 离队集合；
  - NH-11147-02：`TeamSnapshotResponse` 的 `members?` 字段声明未被消费，建议删除
    或补注释说明仅作接口形状文档；
  - NH-11147-03：issue #181 实际归属 GitHub 镜像仓 openJiuwen-ai/jiuwenswarm，
    本仓内同名 #181 为无关文档 issue。
