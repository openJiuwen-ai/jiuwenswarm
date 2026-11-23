---
id: B_20261118_0611_session-mode-switch-retry-and-draft
title: Web 会话内模式切换：重试遇忙核对模式，失败不清空已选技能
type: B
author: Yanqing-Shi
author_email: toddsyq@gmail.com
designs: []
design_note: 共享会话检视（2026-11-17）的两个 P2 问题；实现见 F_20261116_1710_web-session-mode-switch，无独立 D。
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/commitSessionMode.ts
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx
  - jiuwenswarm/channels/web/frontend/tests/commitSessionMode.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/inputAreaPermissionMerge.test.mjs
---

# Web 会话内模式切换：重试遇忙核对模式，失败不清空已选技能

## 现象与影响

1. **首次 mode.set 已提交但回包丢失，重试遇忙后 UI 停在旧模式。** 服务端在校验“已是目标模式”之前先取
   会话锁，取锁失败直接回 `SESSION_BUSY`。前端把重试的 busy 当成两次都未提交，返回 busy，不读回。
   复现：首次请求切到 Team 后回包丢失，另一客户端的聊天取得会话锁，重试得到 busy，服务端已是
   `team.work.normal`，界面仍是 Agent。
2. **切换失败也删除输入框里已选的技能。** `handleModeSwitch` 先清空技能、删除编辑器里的标签并保存输入，
   再异步调用模式切换；服务端拒绝（busy / failed / conflict / uncertain）时会话仍在原模式，草稿已被改动。

## 关联设计与当前契约

实施记录见 `F_20261116_1710_web-session-mode-switch`。服务端的取锁顺序不变（用户选择只改前端）。

## 根因与修复决策

- `commitSessionMode`：重试收到 `SESSION_BUSY` 时调用已有的 `readBack()`。读回已是目标路由则按
  `committed` 处理；读回失败为 `uncertain`；读回仍是旧模式则报 `busy`，不报“已在别处更改”。
- `switchSessionModeInPlace` 返回是否 `committed`；`handleSwitchMode` 返回当前会话是否真的切换
  （草稿为 true，原地切换取前者结果，提前返回与“进入新对话”为 false）。`onSwitchMode` 类型改为
  `Promise<boolean>`。
- `InputArea.handleModeSwitch` 先 `await onSwitchMode()`，为 true 才清理；按会话清空 store 中的技能，
  只有该会话仍为当前会话时才改编辑器。
- 一并修正：可原地切换的版本里，当前模式无法原地切换的真实会话会进入新对话；旧实现也会清掉原会话
  的技能，与原注释“原会话保留”不符，现在保留。

检视时提到的“绑定专家团的会话提前返回也会丢技能”不成立：store 中技能与专家团互斥。

## 回归证据与适用范围

工作区：Swarm 前端（基于 `54d92e149` 加已有未提交改动）。

- `npm run test:session-mode-commit`：新增 3 项（读回仍旧 → busy、读回已是目标 → committed、读回失败 →
  uncertain），修复前失败，修复后 20/20 通过。
- `npm run test:input-area-permission-merge`：新增“切换被拒保留技能与标签 / 切换提交清除”与
  `handleSwitchMode` 返回值用例，并给既有 `switchSessionModeInPlace` 用例补返回值断言；修复后 120 通过、
  2 失败。失败的 `enterprise composer keeps only upload, skills and plan…` 与
  `actual Owner composer remains disabled after stop…` 在不含本次改动的测试文件上同样失败，与本次无关。
- `test:new-conversation-draft-preserve`、`test:recommendation`、`test:enterprise-ui-visibility` 全部通过。

未执行：`tsc` 类型检查与 `npm run build`（本仓约定不跑静态检查）；未在浏览器中手动复现。

## 防错模式与资料同步

异步提交的结果未知时，任何“未提交”的结论都要经读回确认；随提交而来的本地清理放在确认提交之后。

## 遗留与后续

- 可让服务端在取锁失败但已是目标模式时回 `changed=false`，重试即可得到确定结果，不必读回（本次未做）。
- 上述两个既有失败用例需另行处理。
