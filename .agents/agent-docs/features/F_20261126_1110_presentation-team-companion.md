---
id: F_20261126_1110_presentation-team-companion
title: 应用形态支持 Team 会话与成员窗口
type: F
author: zhanglongmin
author_email: zhanglongmin@huawei.com
modules:
  - jiuwenswarm/channels/desktop/electron
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/desktop/electron/application_presentation.cjs
  - jiuwenswarm/channels/desktop/electron/application_presentation_children.cjs
  - jiuwenswarm/channels/desktop/electron/preload.cjs
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/src/features/webui/presentation/ApplicationPresentationHost.tsx
  - jiuwenswarm/channels/web/frontend/src/features/webui/presentation/PresentationFrame.tsx
  - jiuwenswarm/channels/web/frontend/src/features/webui/presentation/conversations.ts
  - jiuwenswarm/channels/web/frontend/src/features/webui/presentation/protocol.ts
  - jiuwenswarm/channels/web/frontend/src/features/webui/presentation/reply.ts
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/tests/applicationPresentation.test.mjs
  - scripts/build-electron-exe.ps1
  - scripts/build-electron-exe.sh
  - tests/electron/application_presentation.test.cjs
designs: []
design_note: '为应用形态增加 Team 会话创建、成员子窗口与队长公开回复；在既有 in-app overlay 双源架构上设计 Host/Frame 管线与成员子窗口协议，无独立设计。'
---

# 应用形态支持 Team 会话与成员窗口

## 实施决策

- 功能范围：成员子窗口（children action）、Team 会话创建（createConversation action）、队长公开回复投影（presentationReplyContent / projectTeamReply）。开发前应用形态为纯 agent 版本（`teamId: null` 硬编码、`mode !== 'agent'` 直接拒绝），本次全部为增量能力。
- **架构基座**：基于既有 in-app overlay 双源架构实现。分层设计——协议层（protocol.ts 追加 action/类型/校验）、数据层（conversations.ts、reply.ts）、Electron 主进程层（application_presentation_children.cjs 及接线）；Host.tsx 管线的 `execute` 采用四参签名 `(request, requestGeneration, windowKey, api)`，staleness 判定使用 `currentRequest(windowKey, generation)` 谓词同时覆盖父窗口与 parentGeneration 匹配的子窗口，ledger 为 per-generation Map，onRequest 以 `activeApi` 双源守卫叠加父子 envelope 路由与 childSync 等待。
- **children × overlay 交叉语义**（确认决策）：仅 desktop 源支持成员子窗口。`overlay.api` 无 `children` 方法，`case 'children'` 的 `if (!api.children) fail('WINDOWS_UNSUPPORTED')` 守卫在双源架构下自然形成"浏览器 / overlay 形态返回 WINDOWS_UNSUPPORTED（提示更新桌面客户端），仅 Electron desktop 形态可开成员子窗口"的行为；不为 overlay 形态新增子浮层路径。
- 复用既有前端能力：`extractTeamLeaderRawContent`、`parseTeamEventMessage`（`isLeaderToUser`/`toMember`）、`enterNewSession({ forceMode: 'team' })`、`setWorkMode` 直接复用，不重复实现。
- `PresentationFrame.tsx` 实现 `isChild` bootstrap 与 `compactPending` 机制：子窗口不发起启动 transition，宿主在 compact 收尾后补发 state；`postState` 改为显式字段组装，`isChild` 不出宿主边界。
- 依赖 `reply.ts` 投影的 team 回复：`team.leader:` 前缀与 `team.event:{"event":{"type":"team.message",...}}` 中队长对用户的公开消息进入应用形态；成员 P2P、定向、监控类事件保持不可见。

## 验证

- `node --test tests/electron/application_presentation.test.cjs`：29 项通过，含本特性新增的 3 项子窗口用例（声明上限与禁带 src、独立子窗口的移动/回复隔离/随父关闭、单子窗口失败不影响其余）。
- `npm run test:application-presentation`（前端 esbuild 预构建 + node --test 4 文件）：60 项通过，其中既有 49 项（含全部 in-app overlay 行为守卫）未破坏，新增 11 项覆盖队长公开回复解码、team 会话快照、createConversation 协议校验与管线时序、子窗口 ledger 隔离。
- `npx tsc --noEmit` 无错误。
- 未完成项：`tests/ui_e2e/application_presentation_overlay.py` 与真实桌面客户端联调（需 EXE 构建环境）未在本次执行；子窗口在真实多显示器环境的几何行为由 Electron 单测的 fixture 模拟覆盖。

## 遗留

- overlay 形态下的"宿主内成员浮层"（若产品需要）是独立新能力，不在本次特性范围，如启动需另立设计。
