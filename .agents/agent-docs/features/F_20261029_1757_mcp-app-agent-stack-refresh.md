---
id: F_20261029_1757_mcp-app-agent-stack-refresh
title: MCP App 到 Agent 的 stacked PR 更新
type: F
author: LI Jinyang
author_email: lijinyang9@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/server/runtime/agent_adapter/user_turn.py
designs: []
design_note: 本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。
---

# MCP App 到 Agent 的 stacked PR 更新

## 关联依据与范围

- 需求：[主仓库 issue 4944](https://gitcode.com/openJiuwen/jiuwenswarm/issues/4944)。
- 基础：[Diego PR !7476](https://gitcode.com/openJiuwen/jiuwenswarm/pull/7476)，更新头 `755cd069b642a37e7d14d7a95f6df981d86bc69c`。
- 当前主线：`ffc539d74ba6fe034c60b6eac5ae3ea1da04b7dc`。
- 原 follow-up：`638c161f9c6f2eab80e8bd05d378ac18c24b82d7`，原基线 `95e0d8df22c42ffe7d3cbc2b82296fd607d4314d`。
- 当前契约：[MCP App 到 Agent 交互规约](../specs/mcpApps/S_01_app-agent-interaction.md)。

## 实际决策与迁移

保留原有 App 文本消息确认和 latest-wins 上下文设计，没有改变功能目标。
仓库既有受管文档中未找到对应实施记录，因此在此记录这次迁移与新验证，不把旧测试结果当作当前证据。

1. Diego 的新分支通过 merge 更新历史，仍包含旧基线。只重放独有 follow-up 提交，不重放其旧基础实现。
2. Diego 分支仍落后主线 21 个提交。为不改机器人分支，先在自己的分支合并当前主线，再重放 follow-up。
3. 解决 package.json 测试脚本和 useWebSocket imports 两处冲突，保留双方新增内容；其余原实现自动适配成功。
4. 当前 MR 目标仍是机器人镜像分支，因此暂时包含主线同步差异。审查独有功能时应以整合基础提交与本提交的差异为准，而不是把所有主线修复归为 follow-up。
5. 最终先合入基础 PR，再将 follow-up 独有功能提交 cherry-pick/rebase 到主仓库 develop，通过面向主仓库的正常 PR 合入。不能把当前镜像 MR 合并当作主仓库交付。

调用链为：iframe → AppBridge → 文本确认 / 实例上下文 → 正常 sendMessage → chat.send metadata → Gateway / AgentServer → UserTurn → 模型输入。
上下文状态只在前端挂载期保存；后端生成带不可信策略说明的输入，不替代用户正文。工具批准与消息确认是不同的入口。

## 当前验证（2026-10-29）

环境：独立 worktree；`npm ci` 和 `uv sync --frozen --python 3.11` 安装锁定依赖；openJiuwen 固定为 `e089198835426cd7681761b0cc278670cd83031f`，不是本地 editable core。

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 实例状态、清理、容量、深度、限流 | `npm run test:mcp-app-context` | 8 项通过 |
| 真实 AppBridge + React 组件，模拟 iframe 消息和后端 RPC | `npm run test:mcp-app-view` | 1 项通过；确认/取消、会话隔离、只读工具仍须批准、卸载清理 |
| 真实 useWebSocket + 内存 WebSocket | `npm run test:chat-attachment-transport` | 权限 15 项、聊天传输 23 项通过；含快照传递、会话隔离和 metadata 保留 |
| 既有会话输入消费者 | `npm run test:session-input` | 63 项通过 |
| 基础 UI 资源与冲突侧脚本 | `npm run test:mcp-app-document` / `test:model-presentation` | 6 项 / 5 项通过 |
| UserTurn、MCP bridge、沙箱 HTTP | `pytest -o addopts='' tests/unit_tests/server/test_user_turn.py tests/unit_tests/test_mcp_apps.py tests/unit_tests/test_mcp_app_sandbox_http.py -q` | 42 项通过、6 项桌面专用测试跳过 |
| 生产构建 | `npm run build` | 通过；存在依赖注释与 bundle 大小警告 |

上述为组件/内存传输/后端单测证据，不是完整浏览器、真实模型或桌面端验收；未执行全量测试和静态检查。
原有 UI 中英文确认文案继续沿用，本次同步中英文使用说明和当前契约。后续候选、依赖或入口变化需重验对应范围。

## 实施演进与修订：2026-10-31 上游正式 PR

- !7476 已合入，基础能力在主线提交 `388089f41`；本次从最新 `develop` 的 `eabe143dbefcae6508beb8acdb3d03eec6f12644` 建立普通功能分支。
- 仅迁移上一轮独有功能提交 `916f02d60`，没有迁移临时主线整合提交 `6ef8f214d`。正文此前的 stacked MR 操作说明与测试结果保留为 10 月 29 日历史，不再作为当前发布状态。
- 当前基础实现已有 `ui/update-model-context` 接收函数，但只保存于组件 ref，没有进入 chat.send 或模型输入；本次补齐其消费链路，并新增有确认的 `ui/message` 路径。
- 迁移没有代码冲突；仅重新生成文档索引以保留主线新增条目。实施 issue 为 [#6037](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6037)，关联整体需求 #4944。
- 当前前端重新验证：`test:mcp-app-context` 8、`test:mcp-app-view` 1、`test:mcp-app-document` 6、`test:model-presentation` 5、`test:chat-attachment-transport` 15 + 23、`test:session-input` 63，共 121 项通过；`npx vite build` 通过，仍有 bundle 大小及依赖注释警告。本轮未运行 tsc、lint 或其他静态检查。
- 后端同一组三个测试文件重新验证为 42 通过、6 桌面专用测试跳过。首次执行因缺少 gateway_protocol 导入失败；当前主线 pyproject.toml 已声明该依赖但 uv.lock 未收录，在虚拟环境中按主线指定版本 `6edf9e36f8de8448b14339712217a263808626bb` 单独安装后通过。该环境准备差异不隐藏，不修改本 PR 以外的主线锁文件。
- 未执行真实模型、完整浏览器及桌面端验收；当前证据范围仍为真实组件/桥接、内存传输和后端单测。原有功能设计与安全边界不变。
