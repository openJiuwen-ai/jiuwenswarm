---
id: B_20261124_2110_rea-use-auto-connect
title: REA 去调用未自动连接 MCP
type: B
author: xiaotian077
author_email: zhongxiaotian@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup/ReaSetupPage.tsx
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup/reaConnector.ts
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/tests/reaConnector.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
designs: []
design_note: 局部修复现有 REA 引导与会话连接语义，不引入新的架构方案。
---

# REA 去调用未自动连接 MCP 修复记录

## 现象与影响

REA 基础能力已安装完成并显示可用，但点击“去调用”后进入新会话时，连接器中的
`rea` 仍显示“连接”按钮，而不是已连接开关。新会话虽然暂存了
`initialEnabledMcps: ['rea']`，发送前会因 `rea` 的连接状态为 `disconnected`
将其过滤掉，导致模型看不到 REA MCP 工具。

实测当前实例时，`mcp.list(filter=local)` 返回：

```text
name: rea
installed: true
connection_state: disconnected
```

`mcp/state.json` 同时显示：

```text
state: registered
enabled: false
```

其中 `enabled: false` 只控制 TUI 默认集合，不是 Web 连接失败的原因；实际根因是
`registered` 不代表 `connected`。

## 关联设计与当前契约

无独立设计。本修复遵循当前连接器契约：

- Web 会话通过 `chat.send.mcp` 启用 MCP。
- 连接器只有在 `connection_state=connected` 时才允许会话启用。
- “去调用”不能绕过连接器，也不能把未连接 MCP 静默带入新会话。

## 根因与修复决策

原实现只执行会话导航：

```text
requestSessionNavigation('new', {
  initialEnabledMcps: ['rea'],
  ...
})
```

它没有调用 `connectorStore.connect('rea')`。发送前
`pruneEnabledExtensions()` 发现 REA 非 connected，会正确移除该项，所以连接器保持
未连接状态。

修复选择：

1. 自动连接仅对 macOS 生效；Windows 继续沿用原有的“只传递
   initialEnabledMcps 并直接导航”业务流程，不调用额外连接接口，也不增加等待态。
2. macOS“去调用”先检查现有连接状态。
3. 已连接时直接导航，避免重复连接。
4. 未连接时调用正式连接器 `connect('rea')`，等待返回 `connected` 后再导航。
5. 连接失败或等待授权时不弹错误提示，并清除本次自动连接写入的全局错误；无论连接结果
   如何都继续进入新会话，由会话连接网关在发送时执行最终能力校验。
6. macOS 等待期间禁用“去调用”及提示词按钮，显示“正在连接 REA…”；Windows
   不进入该等待态。

未选择直接修改 `mcp/state.json` 状态：正式连接器链路还会更新连接器缓存并触发运行时
MCP 注册，直接改文件不能保证当前会话实际可用。

## 回归证据与适用范围

候选环境：

```text
macOS 26.5.2 / arm64
WorkSwarm develop 5a875665f + 本次未提交修复
```

验证结果：

- `npm run test:rea-connector`：4 项通过。
- `npm run test:i18n-locales`：6 项通过。
- `npm run build`：TypeScript 与 Vite 构建通过。
- 浏览器实测：REA 初始为 `disconnected`，点击“去调用”后按钮显示“正在连接 REA…”，
  随后进入新会话并出现“连接器已连接成功”。
- 验证后 `mcp/state.json` 为 `state=connected`，`mcp.list(filter=local)` 返回
  `connection_state=connected`。

未验证范围：

- 连接器自身连接失败后仍进入新会话的行为由单元测试覆盖；未断开 REA 运行时制造真实失败。
- Windows 未重复执行 UI 验证；改动位于跨平台前端连接流程。
- Windows 行为由纯函数测试锁定：`shouldAutoConnectRea(Windows UA) === false`，
  不调用连接器、不显示等待态。

## 防错模式与资料同步

“使用某能力”必须区分会话启用与连接器连接，不能只写入 enabled 名单。当前已通过
`reaConnector.ts` 固化“先尝试连接、连接结果不阻断会话创建”的单一判断，并由纯函数测试
覆盖成功、未返回结果和异常三种路径。

## 遗留与后续

REA 基础安装的 macOS 专属 Frida 预编译准备当前仍是强制步骤。Frida 在 REA 包中是
optional dependency，属于动态分析增强能力；核心 MCP 在原则上不依赖它。当前实现为了
保证动态分析能力而把它当作基础安装成功条件之一。是否改为 best-effort 是独立的产品
取舍，本修复未改变。
