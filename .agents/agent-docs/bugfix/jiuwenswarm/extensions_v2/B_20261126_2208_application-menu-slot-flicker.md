---
id: B_20261126_2208_application-menu-slot-flicker
title: 左侧导航栏 application.menu 槽位切换会话时入口闪烁
type: B
author: kusanagima
author_email: kusanagima@foxmail.com
modules:
  - jiuwenswarm/extensions_v2
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/extensions_v2/webui/constants.py
  - jiuwenswarm/channels/web/frontend/src/features/webui/catalog/slots.ts
  - jiuwenswarm/extensions_v2/docs/specs/S_17_webui-component-props.md
  - jiuwenswarm/extensions_v2/docs/features/F_07_ui-contributions.md
  - jiuwenswarm/extensions_v2/docs/features/F_08_frontend-render.md
  - jiuwenswarm/extensions_v2/webui/README.md
  - jiuwenswarm/extensions_v2/webui/README_EN.md
  - jiuwenswarm/channels/web/frontend/src/features/webui/docs/specs/S_01_components.md
  - jiuwenswarm/channels/web/frontend/src/features/webui/docs/features/F_01_antd-components.md
  - tests/unit_tests/extensions_v2/webui/test_application_slots.py
  - tests/unit_tests/extensions_v2/webui/test_webui_events.py
  - jiuwenswarm/channels/web/frontend/tests/webuiContracts.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/webuiComponents.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/webuiComponentsReact.test.mjs
designs: []
design_note: 依据 GitCode issue #7150 与既有 application-scoped 槽位（task.function / task.function.main）契约实施范围收敛，无独立新增 D 文档。
---

# 左侧导航栏 application.menu 槽位切换会话时入口闪烁修复记录

## 现象与影响

GitCode Issue [#7150](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7150) 报告：通过 `jiuwen-system-plugin-dev` 开发并安装 `agentserver` 域插件 `probe.slot-app-menu`，在左侧导航栏槽位 `application.menu` 渲染「菜单探针」入口，点击后在 `application.menu.main` 打开主页面；当用户在会话之间切换时，左侧边栏中的「菜单探针」按钮会出现先消失再重新出现的闪烁现象（且切换到新建空对话时入口直接消失）。

## 关联设计与当前契约

- 前端槽位目录 `jiuwenswarm/channels/web/frontend/src/features/webui/catalog/slots.ts` 与后端白名单 `jiuwenswarm/extensions_v2/webui/constants.py` 为 WebUI 槽位作用域的唯一定点配置；
- 同类侧栏入口 `task.function` / `task.function.main` 已采用 `application` 作用域，以当前 Web 连接隔离，不依赖聊天 `session_id`；
- 相关规范：`jiuwenswarm/extensions_v2/docs/specs/S_17_webui-component-props.md`、`jiuwenswarm/channels/web/frontend/src/features/webui/docs/specs/S_01_components.md`。

## 根因与修复决策

### 根因链路

1. `application.menu` 与 `application.menu.main` 原先未声明 `application` 作用域，默认归为 `session` 作用域；
2. `App.tsx` 中 `<WebUISlotProvider sessionId={sessionId && sessionId !== NEW_CONVERSATION_ID ? sessionId : null}>` 包裹 `SessionSidebar` 内的 `<WebUINavigationHost slotId="application.menu" />`；
3. 会话从 A 切换到 B 时，`WebUIHost.tsx` 消费的 `sessionId` 变化：
   - 渲染期读取新会话 `sessionB` 的 `scope` 快照，`instanceKeys` 为空，`WebUIHost` 立即 `return null`，按钮从 DOM 卸载；
   - `useEffect` 清理阶段调用 `handle.release()`，`createWebUIRuntime.ts` 在最后一个消费者退出时立即删除旧会话在 `records` 与 `scopeIndex` 中的本地实例缓存；
   - 随后新 `useEffect` 对 `sessionB` 调用 `mountSlot` 与 `notifyReady()`，等待 `agentserver` 响应 `ui.ready` 并下发 `ui.render` 帧后按钮才重新出现，形成肉眼可见闪烁。

### 修复决策与拒绝方案

- **采用方案（方案 A）**：将 `application.menu` 与 `application.menu.main` 统一改为 `application` 作用域（`constants.py` 中使用 `SlotScope.APPLICATION`，`slots.ts` 中配置 `scope: 'application'`），与左侧主导航作为全局应用导航栏的产品定位对齐，并与 `task.function` / `task.function.main` 契约一致。
  - `WebUIHost` 对应用作用域槽位固定 `sessionId = ''`，切换会话或进入新建空对话时不执行 `release()` / `mountSlot()` / `notifyReady()`；
  - `App.tsx` 对 `webUIPage.scope === 'application'` 的页面在切换会话时不清理状态，切回聊天时通过 `.app-section.is-hidden` 仅隐藏容器，保留主页面与 `Iframe` 实例。
- **拒绝方案（方案 B）**：保留 `session` 作用域并在 `WebUIHost` 增加切换会话期间的旧帧占位（stale-while-revalidate）。未采用原因：左侧主导航入口语义上本就不属于单个会话，保留 `session` 作用域不仅会在每次切换会话时无谓触发后端 `ui.ready` 重渲染往返，还会导致新建空对话中导航入口缺失。

## 回归证据与适用范围

- **基线**：`025c27c7aa4d2736663a071f6df86541694258c6`（`develop`），运行环境为 macOS 本地 `.venv`（`uv sync` 同步后）及 `jiuwenswarm/channels/web/frontend`（`npm ci` 后）。
- **后端**：`.venv/bin/pytest tests/unit_tests/extensions_v2/webui/ -q --no-cov`，**614 passed**（`test_application_slots.py` 覆盖 `application.menu` / `application.menu.main` 连接校验、`scope="session"` 拒绝、`apply_slot_scope` 对省略 `scope` 时旧 `session_id` 的归一化及 `ui.ready` 派发渲染；`test_webui_events.py` 覆盖根组件白名单）。
- **前端**：`test:webui-contracts`（179/179）、`test:webui-protocol`（14/14）、`test:webui-runtime`（28/28）、`test:webui-events`（93/93）、`test:webui-renderer`（19/19）、`test:webui-components`（467/467，含 `webuiComponentsReact.test.mjs` 新增的 `application.menu` 无会话挂载与跨会话切换不卸载、不重发 `ui.ready` JSDOM 回归）、`test:webui-plugin-panels`（29/29）、`test:webui-transport`（9/9）、`test:plugin-settings`（17/17）、`test:webui-client`（44/44）及 `npm run build`（`tsc && vite build`）全部通过。
- **空白检查**：`git diff --check` 通过。
- **未验证范围**：尚未在真实运行的跨进程 `WebClient + Gateway + AgentServer` 环境中安装外部插件包（如 `probe.slot-app-menu`）开展浏览器端到端实测。

## 防错模式与资料同步

- **插件契约兼容性提醒**：`application.menu` 与 `application.menu.main` 升级为 `application` 作用域后，其事件 Handler 收到的 `ops.session_context` 为 `None`，插件构造 `RenderTarget` 时应使用 `session_id=""`（或直接复用 `request.target.session_id`）且不得声明 `scope="session"`。此前按会话作用域编写并强依赖 `ops.session_context` 或显式 `scope="session"` 的存量插件需同步移除会话依赖并重载/重装。
- **已同步资料**：`S_17_webui-component-props.md`、`S_01_components.md`、`F_07_ui-contributions.md`、`F_08_frontend-render.md`、`F_01_antd-components.md`、`jiuwenswarm/extensions_v2/webui/README.md`、`README_EN.md`。

## 遗留与后续

- 待测试或联调环境使用遵循应用作用域的 `probe.slot-app-menu` 复验真实浏览器切换会话与页面跳转；
- 注意 `jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/jiuwen-system-plugin-dev/` 受内置技能校验哈希跟踪，本次未修改其示例（其示例槽位仍为 `conversation.input.above`），后续如内置技能模板需补充应用作用域槽位说明可在技能版本更新时一并处理。
