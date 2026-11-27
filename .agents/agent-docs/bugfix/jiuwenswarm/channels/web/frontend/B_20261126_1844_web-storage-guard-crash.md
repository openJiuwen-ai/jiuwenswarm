---
id: B_20261126_1844_web-storage-guard-crash
title: 浏览器存储被禁时 App 首渲染白屏与会话未读写入崩溃修复
type: B
author: linghb
author_email: 957733934@qq.com
designs: []
design_note: 由 GitCode issue openJiuwen/jiuwenswarm#7090 直接驱动的前端局部修复，无独立 D；沿用前端既有的 guarded-storage 防护模式，未引入新契约。
modules:
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/channels/web/frontend/src/hooks
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel
files:
  - jiuwenswarm/channels/web/frontend/src/hooks/useSecondarySidebar.ts
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar/ConversationSidebar.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/MessageItem.tsx
  - jiuwenswarm/channels/web/frontend/tests/secondarySidebar.test.mjs
---

# 浏览器存储被禁时 App 首渲染白屏与会话未读写入崩溃修复

## 现象与影响

GitCode issue openJiuwen/jiuwenswarm#7090：在 Safari 隐私模式（localStorage 配额为 0）或 Chrome 设置"阻止所有 Cookie"的环境下打开 Web 端，App 整页白屏，控制台报 `SecurityError` / `QuotaExceededError`。根因调用点位于 App 外壳顶层首渲染路径，属于 P1 级崩溃；同时 ConversationSidebar 的未读会话持久化写入在同环境下抛错。附带清理：`MessageItem` 渲染 `chat.session_result` 事件时遗留的两处 `console.log` 会把用户会话原文与解析结果打印到生产控制台。

## 关联设计与当前契约

无独立 D。依据的当前事实与既有实践：前端已有同款 guarded-storage 模式——`ConversationSidebar` 的 `loadUnreadSessions`（读方向）、`AppMarketPanel`、`MessageItem` 的 SAVED_SKILLS 段均使用 optional catch binding（`catch {}`，tsconfig target ES2020 支持）；本次将同一模式补齐到剩余崩溃点，未改变任何对外契约。

## 根因与修复决策

**根因**：浏览器禁用存储时 `localStorage.getItem` / `setItem` 不返回 null 而是直接抛异常（Safari 隐私模式配额 0、Chrome 阻止所有 Cookie 均触发）。`useSecondarySidebarPreference` 被 `App.tsx` 顶层消费，其 `useState` 初始化器在 App 首渲染同步执行 `getItem`，异常未捕获直接炸掉整个 React 根，表现为白屏。

**修复口径**：读写双向 best-effort，失败降级为内存态——

- `useSecondarySidebarPreference`：读失败（`getItem` 抛）初始化器返回默认值 `false`；写失败（`setItem` 抛）偏好仅在内存生效。App 首渲染不再崩溃。
- `ConversationSidebar`：未读会话 `setItem` 包 try/catch，与既有防护读 `loadUnreadSessions` 形成读写对称，未读态在内存中继续工作。
- `MessageItem`：删除两处 `console.log`，停止向生产控制台泄露会话内容。

备选方案"抽公共 guardedStorage helper 收敛全前端存储调用"被拒绝：超出 issue #7090 修复边界，作为独立技术债处理（见遗留）。

## 验证

- CI 全绿（PR !10986 评论：静态检查 / 禁用词扫描 / 防投毒 / 开源合规 / UT / build 均 SUCCESS）。
- 回归测试 `tests/secondarySidebar.test.mjs` 新增用例 `blocked localStorage degrades to in-memory defaults instead of crashing first render`：stub `getItem/setItem` 分别抛错，断言首渲染回退内存默认值、写失败后状态内存生效、remount 回退默认值；`npm run test:secondary-sidebar` 4/4 通过。
- 真实浏览器（Chrome 阻止 Cookie / Safari 隐私模式）手工复现未执行，行为结论基于浏览器标准行为（SecurityError/QuotaExceededError）与代码路径推演。

## 遗留

- 前端仍有未防护的 localStorage 调用点（`App.tsx` jiuwenmate-mode、`auth/manager/*` token 读写、`PersonalContext/Intro.tsx`、`features/designer/designerGraphLoad.ts` 等），部分位于认证/首渲染路径，同类崩溃可从这些入口复发；建议独立技术债 PR 抽 guardedStorage helper 统一收敛。
