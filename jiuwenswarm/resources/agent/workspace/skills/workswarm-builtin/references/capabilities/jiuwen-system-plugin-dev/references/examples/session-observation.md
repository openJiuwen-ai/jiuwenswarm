# 会话用量、执行过程和状态条

用户需要上下文用量、Agent 执行过程、工具批次或用量状态条时参考。运行域为 `agentserver`。
以下片段仅供参考，说明事件和 Ops 的组合方式，不是完整系统插件。

## 关键写法

在 `register` 中按需要登记已有事件，不必为一个状态条监听全部执行事件：

```python
registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
registrar.register_event_handler("turn.step", self.on_step, {}, "user")
registrar.register_event_handler("tool.call", self.on_tool, {}, "user")
```

在异步 Handler 中读取当前会话的已观测数据：

```python
usage = await ops.session.usage(breakdown="summary")
model = await ops.session.model()
turns = await ops.session.turns()
```

- **状态条：** 使用 `conversation.input.above`；需要详情时再声明 `conversation.panel.right`，用 `ui.ready` 恢复显示、`ui.click` 处理操作。
- **设置联动：** Token Weather 的关键是应用设置与会话状态条联动。先核对 `application.settings` 和显示偏好协议，不把设置面板自身的会话作用域套到所有会话。
- **页面资源：** 复杂图表可通过 `ops.ui.create_resource_dir` 写入受管目录，退出写入上下文后再用 `ops.ui.asset_url` 发布入口，交给 Iframe。新帧投递成功后释放旧资源，失败或取消时释放新资源。
- **状态与口径：** 按会话隔离历史和执行步骤；未知用量不补零，估算值要说明。不能把一个模型步骤或工具批次当成完整轮次。

只需要状态条时，从用量事件和设置开始；需要执行驾驶舱时，再补步骤、工具及历史。具体字段、资源清理和取消规则按目标版本核对。
