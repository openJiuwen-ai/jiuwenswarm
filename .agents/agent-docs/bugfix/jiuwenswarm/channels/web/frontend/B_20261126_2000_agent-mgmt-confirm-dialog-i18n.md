---
id: B_20261126_2000_agent-mgmt-confirm-dialog-i18n
title: 专家团卸载与专家删除确认框原生 window.confirm 按钮文案无法随应用 i18n 切换
type: B
author: whds5279
author_email: 2755136018@qq.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/components/AgentManagementPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/tests/agentGroupManagement.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/agentGroupUninstall.test.mjs
designs: []
design_note: 交互层缺陷修复，复用既有 ConfirmDialog 组件与 agentManagement.confirm / common.cancel|confirm|loading 文案契约，无独立设计文档。社区 issue 与修复 PR 为事实源：GitCode issue #7000 / PR !11034。
---

# 专家团卸载与专家删除确认框原生 window.confirm 按钮文案无法随应用 i18n 切换

## 现象与影响

专家管理面板中「卸载专家团」「删除专家」走浏览器原生 `window.confirm`。弹窗正文可通过
`t(...)` 本地化，但「确定 / 取消」按钮文案由浏览器/操作系统语言决定，不随应用
zh/en 切换。界面已切英文时，确认框按钮仍可能显示中文（或反之），与周围
ConfirmDialog / 设置页确认框体验不一致。

## 关联设计与当前契约

无独立设计文档。前端已有 `components/common/ConfirmDialog`：`title` / `message` /
`confirmLabel` / `loading` / `onConfirm` / `onCancel`；`loading` 期间禁用确认与取消，
且遮罩点击在 `loading` 时忽略。本修复把专家管理两处确认迁到该组件，不新增交互契约。

## 根因与修复决策

原生 `window.confirm` 只能本地化 `message` 字符串，按钮文案不在应用 i18n 控制范围内。

修复决策（可复用改造模式）：

1. **request / confirm 两段拆分**：点击入口只设 `pending*`（`{ id, name }`），不调 API；
   弹窗确认后再由 `confirm*` 执行删除/卸载。
2. **ConfirmDialog 条件渲染**：有 `pending*` 时挂载弹窗，标题/正文/确认文案全部走 `t(...)`。
3. **三层 busy 守卫**：`loading={busyIds.has(id)}` 禁用双按钮；遮罩点击受组件 `!loading`
   守卫；`onCancel` 内再判 `busyIds`，避免请求在途取消留下半状态。
4. **`finally` 清理**：`clearBusy(id)` 与 `setPending*(null)` 同路径执行（含失败），保证
   弹窗关闭与忙碌态复位，语义对齐旧版一次性 `window.confirm`。

拒绝继续包一层 `window.confirm` polyfill：无法统一样式，也不能复用既有 loading 守卫。

## 回归证据与适用范围

2026-11-26 验证（结论级）：

- `npm run test:agent-group-management`：含 ConfirmDialog 替代断言、四 key `{{name}}`
  占位、源码无 `window.confirm(`。
- `node --test tests/agentGroupUninstall.test.mjs`：vm 行为级覆盖
  `confirmUninstallGroup`（含失败后清空 pending）。
- 范围限定 `AgentManagementPanel` 专家删除与专家团卸载；其它面板仍用原生 confirm。

外部链接：GitCode issue #7000、PR !11034。

## 防错模式与资料同步

同型迁移应复制本模式：`request*` 设 pending → `ConfirmDialog` → `confirm*` 调 API →
`finally` 清 busy + pending；busy 期间禁用确认/取消并挡住遮罩取消。不要在请求开始后
仍允许关弹窗却不清 pending，也不要在 request 阶段直接调破坏性 API。

## 遗留与后续

本 PR 仅为系列迁移起点。前端 `src` 仍有原生 `window.confirm(` 调用点，后续可按上节
模式迁到 ConfirmDialog（或已有专用确认组件）。截至本修复合入时的调用文件：

- `App.tsx`
- `components/AppMarketPanel/index.tsx`
- `components/AppMarketPanel/AppMarketEditorAction.tsx`
- `components/AssetPublishDrawer/index.tsx`
- `components/ChatPanel/index.tsx`
- `components/ChatPanel/InputArea.tsx`
- `components/ChatPanel/MessageItem.tsx`
- `components/PersonalContext/ServicesPanel.tsx`
- `components/SessionsPanel/index.tsx`
- `components/SkillPanel/index.tsx`
- `components/SkillPanel/useEvolution.ts`
- `components/TeamPanel/index.tsx`
- `features/ClawHubSearchModal/index.tsx`
- `features/OnlineSkillSearchPanel/index.tsx`
- `features/SkillEvolutionModal/index.tsx`
- `features/SkillNetSearchModal/index.tsx`
- `features/jiuwenmate/pages/ColleagueProfilePage.tsx`
- `features/jiuwenmate/pages/ColleagueWorkspacePage.tsx`

（`GoalBar/OverwriteGoalConfirmModal.tsx` 仅注释提及，无实际调用。）
