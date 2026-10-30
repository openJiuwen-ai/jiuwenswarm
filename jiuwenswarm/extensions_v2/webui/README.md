# WebUI 插件交互

插件通过 `ops.ui.render` 发布树，并用节点上的 `events` 绑定业务动作。例如：

```python
RenderNode(
    "Button",
    {"label": "刷新"},
    events={"ui.click": {"id": "refresh", "args": {"section": "summary"}}},
)
```

这只声明视图与动作，组件是否能显示取决于宿主是否已接入对应渲染器。
插件端校验支持 16 种非文件组件。
组件列表和字段规则见 [组件规约](../../channels/web/frontend/src/features/webui/docs/specs/S_01_components.md)。

浏览器统一调用 `webui.dispatch`。宿主从已显示的实例和节点补齐请求，插件不应自己生成 node_key 或 revision：

```json
{
  "event": "ui.click",
  "session_id": "s1",
  "target": {"slot_id": "conversation.input.below", "package_id": "pkg.demo", "instance_key": "main"},
  "payload": {"node_key": "0", "revision": 3, "action_id": "refresh", "args": {"section": "summary"}, "data": {}}
}
```

服务端在触发插件 Handler 前校验实例、版本、节点、事件、动作、参数和数据。
Handler 从 `event.event_input.payload["data"]` 读取输入值，`args` 保持声明值，不接收用户编辑内容。
例如 Input 的 `ui.change` 使用 `{value, isComposing}`，Switch 使用 `{checked}`，Slider 使用 `{value}`。
`ui.ready` 使用空 payload，首次挂载时不要求已有实例。

文本输入可这样声明：

```python
RenderNode(
    "Input",
    {"value": "北京", "ariaLabel": "城市", "allowClear": True},
    events={
        "ui.change": {"id": "city.edit"},
        "ui.press_enter": {"id": "city.submit"},
    },
)
```

`ui.change` Handler 收到组合输入过程中的草稿，以及 `isComposing=False` 的提交值。
只处理完整文本时先检查该标志；清空按钮提交空字符串。
`value` 和 `defaultValue` 用于初始化编辑值，合法 replace 会重新初始化草稿，
下行帧校验失败则保留当前输入。disabled、readOnly 或断连时不发送 change。

`ui.press_enter` Handler 收到 `{value}`，取按下 Enter 时的当前编辑值，允许空字符串。
它与 change 独立绑定，可以只声明 Enter；组合输入期间用于确认候选词的 Enter 不触发提交。
disabled、readOnly 或断连时不发送 Enter，每次有效提交独立发送，不等待上一次回执。

需要响应输入框点击时可绑定 `"ui.click": {"id": "city.inspect"}`，Handler 收到当前编辑值 `{value}`。
只读输入仍允许点击；禁用或断连时不发送，未绑定 click 时只保留输入框的默认点击行为。

开关可声明为：

```python
RenderNode(
    "Switch",
    {"defaultChecked": True, "ariaLabel": "启用"},
    events={"ui.change": {"id": "enabled.change"}},
)
```

Handler 从 data 读取布尔值 checked。checked 优先于 defaultChecked；合法 replace 重新初始化状态，
初始化不触发 change。disabled、loading 或断连时开关禁用；未绑定 change 时仍可本地切换。

旧 action 组件继续可用，普通交互同样使用上述请求结构，不接受扁平 payload。
旧 Handler 应把 `payload["value"]` 等读取改为 `payload["data"]["value"]`；List 的选中 key 也在 data 中，
不能再向声明 args 追加 key。单个节点不允许同时声明 action 与 events。
下行帧的 schema_version 为 1。

不支持 application.entry、无会话应用目标或文件组件。请求校验与回执见
[F_07](../docs/features/F_07_ui-contributions.md)。
