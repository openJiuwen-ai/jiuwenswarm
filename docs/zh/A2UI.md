# A2UI 生成式界面

A2UI 是 JiuwenSwarm 的可选生成式界面能力，目前仅 Web channel 原生支持。它让 Agent 在适合表单、确认、结构化详情、多结果比较等场景时输出标准化 UI 消息，由 Web 前端渲染为可交互组件。

## 模块位置

后端 A2UI 代码位于 `jiuwenswarm/server/runtime/a2ui/`。该能力依附 AgentServer 的运行时响应链路，不作为 `jiuwenswarm/` 顶层通用模块存在。

前端渲染代码位于 `jiuwenswarm/channels/web/frontend/src/features/a2ui/`。Web 前端只在渲染 assistant message 和发送交互事件时接入 A2UI。

## 与主模块的关系

| 模块 | 接入点 | 责任 |
| --- | --- | --- |
| `server/runtime/a2ui` | A2UI 后端实现 | 配置解析、Web channel 策略、协议 prompt、响应解析、schema 校验、repair/finalization |
| `agents/harness/common/rails` | response prompt rail | Web channel 且功能开启时注入 A2UI runtime prompt；非 Web channel 或关闭时移除 |
| `server/runtime/agent_adapter` | Agent 输入输出适配 | Web channel 将 A2UI client event 转换为模型可读 prompt，并在完整 assistant response 返回前执行 finalizer |
| `gateway/message_handler` | 消息出口 | 保留兼容 hook；非 Web channel 不执行 A2UI fallback 或 renderer 逻辑 |
| `gateway/channel_manager/web` | Web 配置 API | 暴露 A2UI 总开关，不暴露协议内部参数 |
| `channels/web/frontend/src/features/a2ui` | Web A2UI feature | 解析 `<a2ui-json>` block、注册 renderer、渲染组件、包装 action event、补齐 choice 默认值 |
| `channels/web/frontend/src/hooks/useWebSocket.ts` | 通用传输 hook | 仅提供 `sendStructuredChatContent`，不导入 A2UI feature 代码 |

## 端到端链路

1. Web 用户发送自然语言消息。
2. Agent prompt rail 在 Web channel 且 A2UI 开启时注入 A2UI runtime prompt。
3. 模型在适合场景输出文本和 `<a2ui-json>...</a2ui-json>` block。
4. AgentServer finalizer 校验完整 assistant response；必要时调用 repair prompt 修复。
5. Web 前端解析 assistant message 中的 A2UI block 并渲染组件。
6. 用户点击按钮或提交表单时，前端把 A2UI client event 包装为结构化 chat content。
7. 后端在 Web channel 把该事件转换为模型可读 prompt，进入下一轮对话。

非 Web channel 不进入上述 A2UI 链路：不注入 A2UI prompt，不把结构化 A2UI client event 转换为模型 prompt，不执行 A2UI response finalizer，也不做 A2UI 文本 fallback。它们继续走普通文本/Markdown 对话流程。

## 配置

默认配置位于 `jiuwenswarm/resources/config.yaml`：

```yaml
a2ui:
  enabled: false
  protocol_version: "0.8"
  stream_validation_enabled: true
  non_web_fallback_enabled: false
```

可通过以下方式控制：

- Web 配置页：`A2UI` 顶层开关。
- 用户工作区配置：修改 `config.yaml` 中的 `a2ui.enabled`。
- 环境变量：`JIUWENSWARM_A2UI_ENABLED=false` 或 `true`。

A2UI 默认关闭，需要显式开启后才会向 Web channel 注入 A2UI prompt 并执行 response finalizer。

`non_web_fallback_enabled` 是兼容旧配置的保留字段；当前 A2UI 为 Web-only，非 Web channel 始终 bypass A2UI。

旧的 `JIUWENCLAW_A2UI_*` 环境变量在本 PR 更新后不再支持，弃用时间线为 2026-06-04 起移除兼容；A2UI 运行时覆盖项统一使用 `JIUWENSWARM_A2UI_*` 前缀。

## 依赖版本

后端 SDK 依赖固定为 `a2ui-agent-sdk==0.2.1`，用于保证可重复构建。升级 SDK 时应在同一次变更中更新依赖锁、重新运行协议校验测试，并验证 Web renderer 构建。

## 边界原则

- A2UI 是可选能力，关闭或配置读取失败时不应影响普通文本/Markdown 流程。
- A2UI 的 channel 支持策略由 `jiuwenswarm.server.runtime.a2ui.integration.is_a2ui_channel` 统一定义；当前只有 `web` 返回 true。
- 后端宿主代码只调用 `jiuwenswarm.server.runtime.a2ui.integration` 的薄门面，避免直接依赖协议、repair、schema 等细节。
- WebSocket hook 保持通用结构化消息能力，不导入 A2UI 类型或 renderer。
- Web A2UI 逻辑留在 `channels/web/frontend/src/features/a2ui/`，不散落到通用组件或传输层。
- 非 Web 渠道不感知 A2UI：不需要理解 A2UI renderer、schema、client event 或 fallback 细节。

## 测试覆盖

相关测试位于：

- `tests/unit_tests/a2ui/`
- `tests/system_tests/test_a2ui_system_flow.py`
- `jiuwenswarm/channels/web/frontend/scripts/test-a2ui-action-defaults.mjs`

常用验证命令：

```powershell
uv run pytest tests/unit_tests/a2ui tests/system_tests/test_a2ui_system_flow.py -q
cd jiuwenswarm\channels\web\frontend
npm run build
node scripts/test-a2ui-action-defaults.mjs
```
