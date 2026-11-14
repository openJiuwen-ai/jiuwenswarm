---
id: B_20261113_1416_cron-drawer-double-submit
title: 定时任务抽屉确认按钮无提交中防护，慢响应窗口内双击创建重复任务
type: B
author: gunner_beat
author_email: gunner_heart@163.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/CronTaskDrawer.tsx
  - jiuwenswarm/channels/web/frontend/src/components/CronPanel/drawerSubmitGuard.ts
  - jiuwenswarm/channels/web/frontend/tests/cronDrawerSubmitGuard.test.mjs
designs: []
design_note: 交互层缺陷修复，复用同仓库 HeartbeatPanel 既有 submitting 机制，无独立设计文档。社区 issue 与修复 PR 为事实源：GitCode issue #6561 / PR !10123。
---

# 定时任务抽屉确认按钮无提交中防护，慢响应窗口内双击创建重复任务

## 现象与影响

定时任务面板新建/编辑抽屉的"确认"按钮没有提交中防护。`cron.job.create` /
`cron.job.update` 走 WebSocket 请求（前端 `DEFAULT_TIMEOUT_MS = 15000`），请求
在途期间按钮仍可点击——用户在慢响应窗口内以为没点上而再点一次，会发出第二个
独立请求，创建出两个名称相同的定时任务（编辑场景则重复提交 update）。前端
pending Map 按消息 id 登记请求，不去重，两次请求都会落到后端。纯前端交互缺陷：
服务端正常接收两条合法请求，无数据损坏，但用户侧产生重复脏数据。

## 关联设计与当前契约

HeartbeatPanel 的 `HeartbeatTaskDrawer` 已有同构机制：`submitting` prop 并入
`canSubmit`（`HeartbeatTaskDrawer.tsx:101`），提交中禁用确认按钮。CronPanel
阶段 4 重写抽屉时未跟进该防护，属同一交互契约在 Cron 侧的缺口，本次补齐，
未引入新设计。

## 根因与修复决策

`CronPanel/index.tsx` 的 `handleCreateSubmit` / `handleEditSubmit` 无 busy
标记；`CronTaskDrawer.tsx` 的 `disabled={!canSubmit}` 只含必填校验与"单次"
排班过期校验，无 submitting 条件。

修复决策：

- 拦截语义抽为纯模块 `drawerSubmitGuard.ts`：`createSubmitGuard()` 提供
  `tryBegin()` / `end()`——`tryBegin` 是同步方法，兜底同一事件循环内的双击
  （React state 置位要等下一次渲染才反映到 `disabled`，兜不住同帧连点）；
  `deriveDrawerCanSubmit(missingFieldLabels, scheduleAlreadyExpired, submitting)`
  锁定按钮可用性推导，`submitting` 条件不再可能被后续改动静默弄丢。
- `handleCreateSubmit` / `handleEditSubmit` 进入时 `tryBegin()`，`finally` 中
  `end()` 复位（失败后按钮恢复可用）；state `drawerSubmitting` 经 prop 传入
  抽屉并入 `canSubmit`。
- 与 HeartbeatPanel 口径一致：取消按钮不禁用，用户可在提交中主动关抽屉。

## 回归证据与适用范围

2026-11-13 验证（结论级）：

- 新增 UT 9 用例全绿（canSubmit 四态推导含 submitting 核心条件 / 守卫首次放行、
  在途拒绝、end 复位、幂等、实例独立），入口 `npm run test:cron-drawer-guard`。
- `npm run test:cron-run-notifications` 8/8 通过，与修复前基线一致；
  `tsc --noEmit` 0 新增错误。
- 手动验证：点确认后按钮立即置灰，响应返回前无法再次提交；慢响应窗口内
  连点两次只产生一个任务；提交失败后按钮恢复可用。

外部链接：GitCode issue #6561（现象、复现步骤、验收标准）、PR !10123
（代码评审与流水线记录）。
