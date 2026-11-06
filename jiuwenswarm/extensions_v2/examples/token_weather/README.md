# 上下文用量（Token Weather）

在对话中展示当前会话的上下文用量：每轮回复下方显示本轮用量，输入框下方显示多轮汇总。

- 界面项声明在 `manifest.json` 的 `ui` 段，分别挂在 `message.assistant.meta` 与
  `conversation.input.below`。
- 订阅 `context.usage.updated`，按 `session_id` 累计输入、输出、缓存 token 与上下文占比；
  每次更新后通过 `ops.ui.render` 推送两帧界面树。
- 订阅 `ui.ready`，槽位挂载或页面刷新后立即补发首帧，不必等待下一轮对话。
- 渲染数据只来自事件输入与包内状态，不读取会话文件，也不解析模型输出。

## 生效机制

1. 安装并激活后，插件以 USER 级事件处理器注册上述事件。
2. 平台在每轮对话结束、`chat.usage_summary` 产生后触发 `context.usage.updated`。
3. 插件更新包内状态，并向两个槽位推送 `ops.ui.render` 帧。
4. 前端按槽位把结果渲染在助手消息元信息和输入框下方；刷新页面后由 `ui.ready` 重建。

## 依赖事件

| 事件 | 用途 |
| --- | --- |
| `context.usage.updated` | 接收本轮 token 用量与上下文窗口信息 |
| `ui.ready` | 槽位挂载后补发首帧 |
