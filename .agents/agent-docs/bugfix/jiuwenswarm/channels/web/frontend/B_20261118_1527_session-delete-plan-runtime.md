---
id: B_20261118_1527_session-delete-plan-runtime
title: 侧边栏删除会话漏清 planStore runtime，Plan 模式状态在内存永久残留
type: B
author: gunner_beat
author_email: gunner_heart@163.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar/useSidebarMenu.tsx
designs: []
design_note: 会话生命周期清理的一致性补齐，复用 usePlanStore 既有 removeRuntime，无独立设计文档。缺陷检视清单 #5 为事实源。
---

# 侧边栏删除会话漏清 planStore runtime，Plan 模式状态在内存永久残留

## 现象与影响

删除会话有两条前端清理路径：`App.tsx` 的 `handleTeamSessionsDeleted`（团队批量
删除通知）与 `useSidebarMenu.tsx` 的 `purgeSessionRuntimes`（侧边栏单会话删除）。
前者逐 store 清 9 项 runtime，后者只清 8 项——缺
`usePlanStore.getState().removeRuntime(sessionId)`。

侧边栏删除一个打开过 Plan 模式的会话后，该会话的 plan runtime（`active`、
`pendingExplicitEntry`、`pendingEntrySource`）永久留在内存的 `runtimes` map 里。
纯内存残留：session id 全局唯一不会被复用，残留状态不会影响新会话，但内存占用
随删除会话数单调增长，且两条删除路径的清理范围从此不一致，后续维护者容易在
"以为 purge 是全量清理"的假设下继续加 store。

## 关联设计与当前契约

`usePlanStore` 提供 `removeRuntime(sessionId)`（`planStore.ts`），App.tsx 删除
路径已在用（`handleTeamSessionsDeleted`、NEW 会话切换等三处）。本缺陷属同一
清理契约在侧边栏路径的缺口，本次对齐补齐，未引入新设计。

## 根因与修复决策

`purgeSessionRuntimes` 逐 store 手写清理清单，新增按 session 挂 runtime 的
store 时需要人工同步两条路径——Plan runtime 接入时只同步了 App.tsx 路径。

修复决策：在 `useSidebarMenu.tsx` 的 `purgeSessionRuntimes` 末尾补一行
`usePlanStore.getState().removeRuntime(sessionId)`，顺序与 App.tsx 对齐
（goal 之后、plan 收尾）；store 导入加入 `../../stores` 既有导入块。不改
App.tsx 路径，不抽公共函数（两条路径清理集合本就不同——侧边栏多清
cronSessions 刷新语义，App 路径面向批量删除通知，强行抽象反而耦合）。

## 回归证据与适用范围

2026-11-18 验证（结论级）：

- `tsc --noEmit` 0 新增错误。
- 客户端对照复现（mock 后端 + vite dev + CDP 真实 UI 交互）：打开会话经输入区
  + 菜单打开 Plan 开关（planStore.runtimes 出现该会话条目）→ 侧边栏菜单删除
  会话 → develop 基线 runtimes 中该条目仍残留；修复分支同样操作后条目消失。
  证据截图随 PR 描述。

外部链接：缺陷检视清单 #5（`D:\workspace\output\缺陷检视\2026-11-11-web客户端缺陷清单.md`，
仓外记录）。
