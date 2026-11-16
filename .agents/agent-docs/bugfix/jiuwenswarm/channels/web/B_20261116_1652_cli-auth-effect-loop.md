---
id: B_20261116_1652_cli-auth-effect-loop
title: CLI MCP 授权因父级重渲染重复开窗和请求
type: B
author: TwZhang
author_email: zhangtangwei@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/CliAuthModal.tsx
  - jiuwenswarm/channels/web/frontend/tests/cliAuthModal.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/hostOAuthConnectorModal.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
  - docs/zh/MCP配置.md
  - docs/en/MCPConfiguration.md
designs: []
design_note: 用户明确授权修复现有 CLI 授权生命周期缺陷，无独立设计。
---

# CLI MCP 授权因父级重渲染重复开窗和请求

## 现象与影响

基线为 upstream/develop `51919ff99582af2229cb1d29ce2b944761c1c6f3`。在连接器广场进入 CLI 授权后，
`waitAuth` 同步更新连接器列表，页面重渲染产生新的完成回调，认证 effect 重复开窗和发起长请求。
本地 Edge 浏览器复现记录到 54 次开窗调用、53 次 `mcp.wait_auth`，随后出现
`Maximum update depth exceeded`。验证夹具只实际打开首个本地模拟授权页，其余开窗调用计数并拦截。

受影响的消费者包括 `MarketplacePage`、`McpDetailPage` 和 `PendingConnectorModals`。
后端 `_await_cli_auth` 的默认 200 次、间隔 3 秒轮询会放大每个重复请求的开销。

## 关联设计与当前契约

没有独立 D。现有 `ConnectorConnectResponse`、`connectorApi.waitAuth` 和组件交互要求共同定义行为。
收到 `auth_required` 后自动等待，有 URL 时自动开窗，无 URL 时等待 CLI 自行处理，多步授权按步骤推进，
显式重试重新等待，取消通知后端。

## 根因与修复决策

引入提交 `1d272a7a52610930fdf0a6eb16a27b67fc904d15` 将 `onConnected` 加入授权 effect 依赖。
`connectorStore.waitAuth` 通过 `patchConnectionAll` 同步生成新列表，三个调用方均可能重建完成回调。
`requestSeqRef` 仅能丢弃异步结果，每次重入仍已执行开窗和请求。

- 用单独 effect 更新 `onConnectedRef` 与 `tRef`，认证 effect 只随授权参数、显式重试及稳定 action 变化。
- 以连接器名、步骤和地址记录自动开窗。手动打开按钮继续允许用户再次打开。
- 弹窗内复用当前步骤和重试轮次的等待 Promise，host OAuth 的启动 Promise 同样复用。
  StrictMode effect 重放会重新订阅现有请求，旧订阅通过序号失效。
- 取消及 effect 清理使旧结果失效。显式重试生成新的请求轮次。
- 保留 host OAuth 后端信号及 `mcp.oauth.start` 流程，并补充启动失败重试和完成回调更新验证。

曾考虑只在各页面使用 `useCallback`，但共享弹窗的授权生命周期仍会依赖消费者的函数身份。
曾考虑仅移除依赖，仍需要处理最新回调、语言变化和 StrictMode 重放，因此在共享弹窗集中维护请求生命周期。
Promise 复用限定于单个已挂载弹窗，跨弹窗、取消后重新连接的服务器幂等语义沿用现有实现。

## 回归证据与适用范围

验证环境为 Windows、Node 24.13.0、npm 11.6.2 和系统 Edge 的 ChatGPT 扩展。
候选为上述基线加本记录列出的代码及测试差异。

- 修复前运行新增回归集，10 项中 9 项失败，其中 CLI 路径报告最大更新深度错误，host OAuth StrictMode 路径未续接等待。
- 修复后 `npm run test:cli-auth-modal` 11 项通过，覆盖父级订阅刷新、最新完成回调、StrictMode、无 URL、多步骤、失败重试、手动打开、语言切换、取消、卸载和 host OAuth。
- `npm run test:host-oauth-connector-modal` 6 项、`npm run test:pending-connector-flow` 4 项、
  `npm run test:connector-store-uninstall-guard` 1 项及 `npm run test:equipment-marketplace` 4 项通过，共 26 项通过。
- 浏览器从真实 `MarketplacePage` 的连接按钮、`McpDetailPage` 的连接按钮、
  `usePendingConnectorFlow` / `PendingConnectorModals` 进入，保留真实 Zustand action 与 `connectorApi` 响应转换，
  在 `webRequest` 传输边界接入本地模拟 RPC。三个入口等待期间触发 10 次父级刷新，均保持一次开窗和一次等待，
  模拟授权成功后关闭弹窗并完成消费方续跑。
- Canva host OAuth 在 StrictMode 与语言切换场景保持一次开窗及一次等待，并成功关闭弹窗。

浏览器截图随 PR 提供。该验证覆盖前端生命周期和 RPC 次数，飞书、钉钉、Canva 的真实账号授权与 CLI 子进程未执行。
遵循仓库本地约束，验证使用行为测试与浏览器打包，不运行格式化及静态检查。

## 防错模式与资料同步

对带开窗及长请求副作用的授权 effect，回调和翻译更新通过 ref 传递，
步骤身份决定副作用生命周期。回归测试通过真实 store 同步写入触发父级重渲染，并断言副作用次数。
组件内注释、此故障记录及中英文 MCP 文档维护对应约束。

## 遗留与后续

真实第三方授权环境验证未执行。取消后 store 对迟到服务器结果的状态竞争属于现有跨请求语义，
本次验证确认弹窗不会在取消或卸载后调用完成回调。
