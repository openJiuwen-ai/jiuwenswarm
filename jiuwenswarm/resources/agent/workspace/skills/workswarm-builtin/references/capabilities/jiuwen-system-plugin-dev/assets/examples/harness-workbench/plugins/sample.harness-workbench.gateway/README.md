# Harness 工作台（Gateway）

按浏览器连接记住 Harness 选择，即时预览 Claude / Codex 主题，并在新会话首条消息注入对应专家模板。

## 功能

- `application.background` 在选择 Claude 或 Codex 时展示品牌背景预览；普通对话恢复宿主默认外观。
- 通过 `ops.appearance` 下发受控强调色，不注入宿主页面 CSS。
- 新会话创建后，仅对接下来的**一次**首条 `chat.send` 写入 `agent_template_name`。

## 生效机制

1. `gateway.request` 处理入站帧：从 `webui.dispatch` 同步 AgentServer 侧栏的 Harness 选择，在 `session.create` 后武装下一次首条发送，并在符合条件的 `chat.send` 上一次性改写模板字段。
2. `gateway.disconnect` 在浏览器连接关闭时清理连接级选择与武装状态。
3. `ui.ready` 在 `application.background` 挂载时恢复当前连接的主题预览与会话已保存的主题。

需与 AgentServer 包 `sample.harness-workbench` 配合：侧栏切换与本包的主题、首条注入分工协作。

## 优势

- 在首条消息绑定前后均可预览背景与强调色。
- 首条绑定后会话主题持久化，刷新或重开会话后自动恢复。
- 不改写已有模板或已发过消息的会话。
