# 任务 5：前端跨群分享审批实施与验证记录

日期：2026-10-28。

结论：sharing 功能及指定回归 **95/95 通过**；首次生产 build 退出 2，后续主进程最小兼容修复后的生产 build **退出 0**。真实平台/浏览器联合验收未执行。以下保留初次失败证据，最终状态见文末补验。

## 工作范围与实施

直接编辑 `C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend`，按用户要求不创建 worktree、不提交、不启动服务、不安装或修改依赖/锁文件、不运行 formatter、lint 或独立静态检查命令。唯一的 TypeScript 编译由允许的 `npm --ignore-scripts run build` 触发。

已读目标 root、web、frontend、ui AGENTS 及指定设计/实施计划。源码以旧仓明确指定的 sharing 文件和增量为参考，保留新仓其他实现。未加入旧仓额外 WeLink connection 类型或解绑 API；API 文件仅增加 sharing 工厂导入和 `sharing` 成员。

- 新增 `groupSharingApi.ts`：使用现有 RPC request，保留 `sharing.list/decide/revoke` 方法和 snake_case 参数；`agent_id` 使用所有者配置/Web ID，拒绝错误 owner/conversation 快照；query 为完整对象，正文 `text` 为 string/null。
- 新增 `useGroupSharing.ts`：独立于普通 Requests 的列表刷新、轮询、重连、焦点恢复、期限更新；操作期间阻止重复动作和列表覆盖；响应丢失后先查询实际状态，失效实例忽略迟到响应。
- 新增 `GroupSharingInbox.tsx/.css`：所有者 Web 的 `sharing-approvals` 专用会话；一次/永久/拒绝、可展开确切正文、期限状态、永久授权表与撤销。普通私聊仅显示待审批数量和入口；IM workspace 不查询或呈现卡片。审批会话不加载普通聊天历史、无聊天输入、不发送模型请求或普通工具审批。
- 普通动作复用现有 `Button` 的禁用、焦点、键盘行为；审批状态与私有卡片布局保留在 sharing 特性，不修改共享 UI。CSS 仅使用现有语义主题变量，没有新增产品颜色常量。
- 仅新增 zh/en `jiuwenmate.sharing` 子树；通过嵌套 key 和插值字段一致性测试，包含完整查询条件标签与 null 正文占位。其他 locale 数据经对比保持一致。
- `package.json` 仅增加 `test:group-sharing`，原 scripts、dependencies、devDependencies 保持一致。
- 不修改 frontend 之外的文档或后端。用户补充后端 repo/Gateway 71 pass 为外部提供的信息，未计入本轮 95 个前端测试。

## RED → GREEN

1. 先移植 `tests/buildGroupSharing.mjs` 和 `tests/groupSharing.test.mjs`，新增 package script。将 sharing 模块显式加入构建入口，在复制产品文件之前执行 `npm --ignore-scripts run test:group-sharing`，退出 1：`groupSharingApi.ts`、`useGroupSharing.ts`、`components/GroupSharingInbox.tsx` 三个模块无法解析。原始输出见 `red.log`。
2. 移植四个产品文件及 API/private-chat/workspace/locale sharing 增量后，旧仓 21 项功能测试全部通过，见 `green-initial.log`。
3. 增加 5 项功能/契约验证。旧代码对 legacy string query 的测试出现 `Missing expected rejection`（其他 4 项通过），见 `edge-red.log`；收紧为设计要求的完整 query 对象后，26/26 通过，见 `group-sharing.log`。
4. 一次从仓库根目录误触发 npm 得到 ENOENT（根目录无 package.json），已改为下面列出的 frontend 工作目录执行；该次不计入功能测试结果。

## 实际命令、退出码与日志

所有 npm 命令工作目录均为本文开头的 frontend 绝对路径。可复制执行：

```powershell
Set-Location -LiteralPath 'C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend'
```

原始日志保存在该目录 `node_modules/.cache/group-sharing-evidence/`，没有放入待提交日志文件。机器可读结果见同目录 `results.json`。

| 命令 | 实际结果 | 原始日志 |
| --- | --- | --- |
| `npm --ignore-scripts run test:group-sharing` | 26/26，退出 0 | [group-sharing.log](<C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend/node_modules/.cache/group-sharing-evidence/group-sharing.log>) |
| `npm --ignore-scripts run test:jiuwenmate-api` | 29/29，退出 0 | [test-jiuwenmate-api.log](<C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend/node_modules/.cache/group-sharing-evidence/test-jiuwenmate-api.log>) |
| `npm --ignore-scripts run test:jiuwenmate-chat-switch` | 2/2，退出 0 | [test-jiuwenmate-chat-switch.log](<C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend/node_modules/.cache/group-sharing-evidence/test-jiuwenmate-chat-switch.log>) |
| `npm --ignore-scripts run test:jiuwenmate-profile` | 24/24，退出 0 | [test-jiuwenmate-profile.log](<C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend/node_modules/.cache/group-sharing-evidence/test-jiuwenmate-profile.log>) |
| `npm --ignore-scripts run test:jiuwenmate-workspace` | 8/8，退出 0 | [test-jiuwenmate-workspace.log](<C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend/node_modules/.cache/group-sharing-evidence/test-jiuwenmate-workspace.log>) |
| `npm --ignore-scripts run test:i18n-locales` | 6/6，退出 0 | [test-i18n-locales.log](<C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend/node_modules/.cache/group-sharing-evidence/test-i18n-locales.log>) |
| `npm --ignore-scripts run build` | 7 个 TypeScript 错误，退出 2 | [build.log](<C:/yzj/files/open_source/jiuwenclaw_4.21/jiuwenclaw/jiuwenswarm/channels/web/frontend/node_modules/.cache/group-sharing-evidence/build.log>) |

## Sharing 功能测试明细

1. sharing RPCs use the owner config ID and fixed snake_case payloads through the existing request channel
2. a never-used private chat discovers a durable inbox without exposing previews or invoking a model
3. once updates the persistent card and prevents duplicate decisions while in flight
4. permanent updates the persistent card and prevents duplicate decisions while in flight
5. reject updates the persistent card and prevents duplicate decisions while in flight
6. permanent source-to-destination grants show their scope and can be revoked with error and in-flight handling
7. expired, denied and decided cards stay visible with disabled actions
8. polling discovers new requests, reconnect refreshes, and reopening restores records without normal chat history
9. list failures are recoverable and block stale actions; decision errors never claim approval
10. IM workspaces never query or render sharing cards, and late Web results cannot leak there
11. an invalid private conversation or foreign config snapshot fails closed
12. sharing UI has matching Chinese and English resources and renders translated actions
13. a backend-created private conversation opens approval cards even with zero ordinary Requests
14. polling cannot overwrite an in-flight decision, and a lost committed response is reconciled before retry
15. deadlines expire locally even when the server keeps returning pending, including epoch timestamps
16. late decision responses cannot repopulate an IM workspace
17. the global conversation poll discovers an unselected colleague inbox without a Request or a preview stream
18. a pending card is discoverable before native bind, but becomes actionable only after can_decide is refreshed
19. repository owner views preserve the complete GroupMessageQuery and nullable message text across all sharing RPCs
20. the private card shows readable query criteria and a neutral placeholder for a null message body
21. a filters-only repository query and a null target app ID render with Chinese labels
22. sharing RPCs reject legacy string or incomplete queries instead of accepting a different approval scope
23. a late list response from before reconnect cannot restore pending over a newer decision
24. revocation blocks duplicate clicks and polling, and its late response cannot populate an IM workspace
25. owner workspace decisions and revocation always address the configuration and never send an ordinary approval chat
26. all nested sharing translations and interpolation fields match in Chinese and English

## Build 失败的准确范围

`npm --ignore-scripts run build` 执行 `tsc && vite build`，TypeScript 阶段退出 2，Vite 未执行。没有修改 tsconfig 或降低编译要求来绕过错误。

- `src/App.tsx:491`：`rsiRailExpandNonce`、`setRsiRailExpandNonce` 未使用（2 项 TS6133）。
- `src/App.tsx:4455`：传入 `externalCliInstallBusy`，但现有 `SettingsPage` props 无此字段（TS2322）。
- `src/features/trajectory/trajectoryArchive.ts:16`：无法解析 `fflate`（TS2307）。现有 package.json 已声明 `fflate: 0.8.3`，本地 `node_modules/fflate/package.json` 不存在。
- 同文件 `:671`：回调参数 `error`、`data`、`final` 缺少可推断类型（3 项 TS7006）。

App.tsx、SettingsPage.tsx 和 trajectoryArchive.ts 与 HEAD 内容一致（仅规范换行后对比），证据见 `build-baseline-evidence.txt`。本轮 sharing 文件没有出现在编译错误列表中。遵循 sharing-only/不碰依赖的要求，未修改这些既有文件，也未安装缺失包。未执行另一个隔离基线构建，归因证据是实际错误位置、未改动源码对比和已安装依赖状态。

## 最终审阅与限制

实现者和主进程复核了 sharing 差异、API 契约、异步生命周期、私聊/IM 隔离、UI 复用、主题变量和 i18n。最终独立审查子进程因账号额度中断，不能将自审视为独立审查结果。

未启动真实 UI/后端服务，没有进行真实平台分享审批或 E2E；后端由用户主进程负责。没有把本轮内容之外的并行后端改动归为本轮产物。生产 build 的现有阻塞如实保留。

## 主进程最终补验

主进程仅删除 `App.tsx` 中未使用的 RSI state 和已经失效的 `externalCliInstallBusy` prop；从本机已有安装恢复本地缺少且已声明的 `fflate 0.8.3`。依赖清单、锁文件没有因此改变，没有修改 tsconfig 或降低编译要求。

- `npm --ignore-scripts run build`：退出 0，Vite 构建完成；日志为本机临时目录 `new-sharing-ui-build-final.log`，保留既有大 chunk 警告。
- `npm --ignore-scripts run test:group-sharing`：再次执行 26/26 通过，日志 `new-sharing-ui-functional-final.log`。
- `test:external-cli-install-state`：19/19 通过；`test:rsi-presentation`：15/15 通过。
- 因而 sharing 及相关 UI 回归合计 129 项通过。首次失败日志和上述 95 项明细仍作为历史证据，不代表最终 build 状态。

## 现场反馈后的静默刷新与提问者姓名修复

2026-10-28：后台轮询已有卡片时不再反复设置 loading；保留首次加载和手动刷新状态。新增测试证明慢请求期间 aria-busy 为 false、批准按钮保持可用、卡片节点及展开内容保留。后台读取错误仍禁用旧动作，真正提交时仍阻止重复操作并核对服务端状态。

服务端查不到目标群中的提问者姓名时返回空显示标签；组件改用完整中英文占位，不显示原始 ID，身份及授权键仍由服务端保存。

本轮先观察新增闪动测试失败，然后 sharing 27/27、i18n 6/6、workspace 8/8 通过。再次 `npm --ignore-scripts run build` 退出 0，Vite 用时 1m58s，保留大 chunk 警告；本机临时日志为 `sharing-ui-flicker-red.log`、`sharing-ui-flicker-green.log`、`sharing-ui-locales-final.log`、`sharing-ui-workspace-final.log`、`sharing-live-ui-build.log`。没有进行真实浏览器联合验收。

## 审批对话与长期授权页面调整

2026-10-28：将 `sharing-approvals` 改为私聊式审批时间线。待处理请求显示为 Agent 消息；已批准、永久批准和拒绝仅根据持久状态显示为所有者消息气泡，不调用普通聊天发送接口或触发 Agent。过期及不可审批的请求不显示决策按钮。长期授权列表和撤销入口移到对应 Agent 的权限页，撤销记录保留且不可重复撤销。

- `npm run test:group-sharing`：26/26 通过，覆盖消息时间线、三种决策气泡、刷新和重开恢复、不可审批状态、失败与重复提交、IM 隔离及中英文文案。
- `npm run test:jiuwenmate-profile`：26/26 通过，覆盖当前 Agent 的配置 ID、授权范围／状态／时间、撤销、加载失败及撤销失败。
- `npm --ignore-scripts run build`：退出 0，Vite 构建完成；保留动态导入与大 chunk 警告。
- 未启动服务，也未进行真实飞书／钉钉或浏览器联合验收。
