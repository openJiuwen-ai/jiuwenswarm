---
id: B_20261030_1250_org-snapshot-removes-mirrored-workers
title: 组织 UI 快照投影整表覆盖导致镜像 workflow worker 从成员面板消失
type: B
author: xiajing
author_email: xiajing22@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/stores/teamSliceApply.ts
  - jiuwenswarm/channels/web/frontend/src/stores/teamSelectorStore.ts
  - jiuwenswarm/channels/web/frontend/tests/teamSliceApply.test.mjs
designs: []
design_note: 对既有"快照完整替换"契约（跨 Team 切换不串数据）的成员级例外修复，属 F_20261027_1026 特性线的验证期 bugfix，无独立设计文档。
---

# 组织 UI 快照投影整表覆盖导致镜像 workflow worker 从成员面板消失

## 现象与影响

开启团队组织 UI（`experimental.team_organization_ui_enabled`）时，进入会话或刷新
页面后，成员面板上 swarmflow run 的镜像 worker 全部消失，只剩真实成员；任务面板
的 run 卡正常。已结束 run 的 worker 消失后**不会自愈**：镜像 worker 只在 worker
启动时发一次 `team.member.spawned`，历史回填通常翻不到几小时前的 worker 事件，
面板会一直空到下一次有新 run 启动。

镜像 worker 是前端概念：`team_helpers._workflow_updated_to_team_events` 把每个 run
的每个 workflow agent 转成一条 `team.member.spawned`（member_id = `<run_id>:<agent_id>`，
带 `workflow_run_id`），它们只存在于客户端 store，从不进 team.db 名册。

## 关联设计与当前契约

本仓 F_20261027_1026 特性线引入镜像 worker 成员（run 分组展示依赖它），该特性的
"快照对账不删镜像成员"契约此前只覆盖两条路径：WS 重连对账
（`useWebSocket.ts` 走 `reconcileSnapshotRoster`，带 `workflow_run_id` 的成员豁免
删除）与历史面板回填（`teamHistoryPanelRestore.ts` 保留已知字段）。本次是第三条
替换路径（组织 UI 快照投影）漏网，同契约补齐，未引入新设计。

## 根因与修复决策

`stores/teamSliceApply.ts` 的 `applyTeamSnapshotToSession` 用 team.db 名册**整表
替换** `teamMembers`。触发链：`TeamSelector` 的自动选队 effect 在每次进会话/刷新
后调 `selectTeam` → 请求带 `team_name` 的 `team.snapshot` → 服务端从 team.db 返回
真实成员（不含镜像 worker）→ 整表写回把镜像成员当残留清掉。"必须完整替换"的
原意是给**切换 Team** 用的（不能把上一个 Team 的成员带过来），对 owner team 把
"只存在于前端的镜像成员"也一并当成了上一个 Team 的残留。

修复决策：

- 写回保留"当前投影里带 `workflow_run_id`、且不在快照名册里"的成员，镜像成员
  前插（与 `addTeamMember` 前插语义一致，run 分组顺序由成员首次出现位置决定）。
- **凭据必须是 `workflow_run_id`**：不能改成"快照里没有就保留"，否则真正离队的
  成员会被留在面板上、快照的清理职责失效（保留的判定恰好排除无此字段的离队成员）。
- 同 `member_id` 两边都有时**以快照为准**，不泄漏镜像字段。
- **空快照仍完整替换**：跨 Team 切换"空快照清空"是既有契约（team-organization
  既有用例明确断言），镜像豁免不破它。
- 专家团（`is_owner=false`）走独立 conversationKey，那个投影里天然没有镜像成员，
  保留规则不影响切 Team 的完整替换原意。
- **独立实现而非复用 `reconcileSnapshotRoster`**：该函数"无可对账成员时返回 null
  （保持现状）"的语义与这里"空快照也要写回"相反，套用要显式反处理 null，分支数
  不降反升；真正同源的只有一行判据，不值得引一层接口。注释标注"规则同
  reconcileSnapshotRoster"以保持口径可追溯。
- 顺手补齐（前向兼容）：成员映射此前只取 7 个基础字段、不带 `workflow_run_id`
  （也无 `agent_template` / `display_name`）——后端快照将来即使带上镜像成员，
  落进 store 也会变"未分组"。映射补这三个字段透传；当前服务端快照成员形状确实
  还不带这些字段（live 与 DB fallback 两处形状一致），此改动是为后端演进预留落点。

## 回归证据与适用范围

2026-10-30 验证（结论级）：

- 新增 UT 5 用例红→绿（镜像保留且原样透传 / 真离队成员仍被清 / 同 id 冲突以快照
  为准 / 空快照写回时镜像仍保留 / 新字段透传），入口
  `npm run test:team-slice-apply`。
- 直接覆盖 `applyTeamSnapshotToSession` 的既有 `test:team-organization` 88/88 通过
  （含"空快照清空 / 跨 Team 切换不串数据"契约）。
- departure 6/6、merge 2/2、run-grouping 9/9、team-history-panel 9/9 回归与基线一致；
  `tsc --noEmit` 0 错误（修复前基线同为 0）。

未验证范围：手动 ST（开组织 UI 实看成员面板，含切 Team 回切、真实成员 shutdown
后重连仍能清掉）未跑，留终验。

## 防错模式与资料同步

成员投影的"快照替换"共有三条路径（WS 重连对账 / 历史面板回填 / 组织 UI 快照
投影），排查时容易只修到一两条——本次即第三条漏网。后续改动成员投影替换语义时
需三条路径同查。本记录即契约落点，无需另立 spec。

## 遗留与后续

- 手动 ST 留终验（进有已结束 run 的会话 / 切 Team 回切 / 真实成员 shutdown 后重连
  三步走一轮）。
