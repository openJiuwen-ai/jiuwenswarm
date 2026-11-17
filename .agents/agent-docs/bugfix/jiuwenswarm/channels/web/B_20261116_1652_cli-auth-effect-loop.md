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
- 以连接器名、步骤、地址和显式重试轮次记录自动开窗。手动打开按钮继续允许用户再次打开。
- 弹窗内复用当前授权响应轮次的等待 Promise，host OAuth 的启动 Promise 按显式重试轮次复用。
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

## 2026-11-16 PR 10279 检视修订

此次续写对应同一故障修复的检视反馈。检视候选为 `0fa95887b5ed7b84e0e9fd25895fab3835157244`，
反馈来自 PR 10279 的行评论 201086238 和审查摘要 201082076。

已确认同一步骤再次返回 `auth_required` 时存在等待停滞。
URL 相同会使 effect 缺少新的触发条件，URL 改变时又会复用已经完成的等待 Promise。
因此把授权响应与自增 `round` 放进同一状态，每次 CLI `auth_required` 响应产生新的等待轮次，
等待键包含连接器名、步骤、地址、显式重试轮次及授权响应轮次。
同一轮的父级刷新及 StrictMode effect 重放复用原请求，收到后端续接响应后发起新请求。
同一步骤续接时只在步骤序号实际递增后显示上一授权步骤完成提示。

显式重试的自动开窗键补入 `retrySeq`，恢复用户点击重试后重新打开当前地址的行为。
同一步骤连续返回仍沿用同一开窗记录，地址变化时打开新地址。
只把 URL 加入等待键无法覆盖重复响应，因此同时引入响应轮次。

host OAuth 的 `waitAuth` 返回 `auth_required` 时原实现已进入错误态。
此次沿用该终态处理，新增回归确认错误显示、显式重试创建新 flow、同 URL 重开及最终完成回调。
检视摘要提到的 lint 验证受仓库禁止运行静态检查的约束，继续采用行为回归。

修订前先运行扩展后的组件测试，18 项中 9 项失败。
失败覆盖六组同一步骤响应续接、CLI 重试开窗、RPC 失败重试开窗以及 host OAuth 同地址重试开窗。
修订后执行以下回归，共 33 项通过。

- `npm run test:cli-auth-modal` 18 项通过。
- `npm run test:host-oauth-connector-modal` 6 项通过。
- `npm run test:pending-connector-flow` 4 项通过。
- `npm run test:connector-store-uninstall-guard` 1 项通过。
- `npm run test:equipment-marketplace` 4 项通过。

新增续接回归覆盖相同地址、新地址和无地址，以及普通模式和 StrictMode。
每组连续两次返回同一个响应对象，再触发父级刷新，断言每个已完成响应只续接一次等待，最终可连接成功。
本轮证据来自真实 React 组件及 Zustand action 的 jsdom 行为测试，浏览器截图保留为前一候选的历史验证。

## 防错模式与资料同步

对带开窗及长请求副作用的授权 effect，回调和翻译更新通过 ref 传递，
步骤身份决定副作用生命周期。回归测试通过真实 store 同步写入触发父级重渲染，并断言副作用次数。
组件内注释、此故障记录及中英文 MCP 文档维护对应约束。

## 遗留与后续

## releases/0.2.9 双合验证

回补基线为 `583cb595daa81bc1e9a7ecef03b9335c46f071f9`。
授权弹窗、组件回归和中英文说明与 develop PR 10279 的修复一致。
在独立 worktree 上运行五组前端行为回归，共 33 项通过。
测试使用已有 frontend node_modules 依赖，打包时观察到基线中英文语言文件的
`qwenOnly`、`computerControl`、`requiredFields` 重复键警告。
真实第三方账号授权及完整发布分支 CI 仍待验证。

真实第三方授权环境验证未执行。取消后 store 对迟到服务器结果的状态竞争属于现有跨请求语义，
本次验证确认弹窗不会在取消或卸载后调用完成回调。
