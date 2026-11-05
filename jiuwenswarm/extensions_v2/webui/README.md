# WebUI 插件交互

完整组件属性统一维护在 [S_17 Props 权威规约](../docs/specs/S_17_webui-component-props.md)，覆盖全部字段、默认值、嵌套结构、事件和受影响功能。
插件声明、后端校验与前端渲染都遵循该文件；变更先更新规范，再同步三端和调用方。本文保留使用示例，不另行定义属性全集。

Button 和 Text 的文案字段统一为字符串 `value`，不接受 `props.children`；适配器将 `value` 渲染为 React 子内容。节点级 `children` 仅用于允许嵌套的容器组件。

插件通过 `ops.ui.render` 发布树，并用节点上的 `events` 绑定业务动作。例如：

```python
RenderNode(
    "Button",
    {"value": "刷新"},
    events={"ui.click": {"id": "refresh", "args": {"section": "summary"}}},
)
```

这只声明视图与动作，组件是否能显示取决于宿主是否已接入对应渲染器。
插件端校验支持 21 种目录组件（含 `Avatar`、`FileTree`、`FilePreview`、`Dropdown` 与 `Icon`）。
组件字段见上述权威规约；运行和交互约束见 [组件规约](../../channels/web/frontend/src/features/webui/docs/specs/S_01_components.md)。

无边框的文字按钮使用 `type="text"`，hover 背景由宿主主题统一提供。Button、Dropdown 与 Icon 共用约 100 个 Lucide 图标及兼容别名 `refresh`，
完整名称见前端唯一维护入口：[icons/whitelist.json](../../channels/web/frontend/src/features/webui/icons/whitelist.json)；
需要纯图标按钮时省略 `value` 并设置 `icon`，同时保留非空 `aria-label` 作为可访问名称与悬停提示。
例如 `{"type": "text", "icon": "refresh", "aria-label": "刷新"}`。
聊天头部按插件声明渲染按钮，其他动作不会自动显示为刷新。

独立展示图标使用 `Icon`，`name` 只接受白名单名称，可选 `size` 与可访问名：

```python
RenderNode("Icon", {"name": "search", "size": "large", "aria-label": "搜索"})
```

省略 `aria-label` 时按装饰图标处理，不进入可访问树；`size` 使用 small/medium/large（兼容 sm/md/lg）。

维护图标时只编辑仓库路径 `jiuwenswarm/channels/web/frontend/src/features/webui/icons/whitelist.json`。
`icons` 将插件使用的 kebab-case 名称映射到 Lucide React 导出名，`aliases` 将旧名称映射到 `icons` 中的名称。
在前端目录运行 `npm run generate:webui-icons`，会自动生成静态导入表及后端 `icon_whitelist_generated.py`；
前端 dev/build 也会自动执行。生成文件一并提交，不手工修改；后端部署不需要 Node 或前端源码。
具体维护与检查步骤见[前端文档](../../channels/web/frontend/README.md#插件-webui-图标白名单)。

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
`ui.ready` 通常使用空 payload，首次挂载时不要求已有实例；`application.entry` 使用 `{"reason": "first_visit"}`。

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
开关也可独立绑定 `"ui.click": {"id": "enabled.inspect"}`，接收切换后的 `{checked}`。
同时绑定 change 和 click 时，一次点击依次发送 change、click；只需处理开关状态变化时绑定 change 即可。

选择框可以同时绑定选择、搜索和清空动作：

```python
RenderNode(
    "Select",
    {"options": [{"value": "bj", "label": "北京"}], "showSearch": True, "allowClear": True},
    events={
        "ui.select": {"id": "city.choose"},
        "ui.search": {"id": "city.search"},
        "ui.clear": {"id": "city.clear"},
    },
)
```

三项分别发送 `{value}`、`{searchValue}` 和 `{}`，可以按需省略绑定。
多选时 select 仍发送本次选中的单个选项值。可选绑定 `"ui.change": {"id": "city.change"}`
接收完整选择值 `{value}`，包含取消选项和清空；单选为字符串或数值，清空为 null，多选/tags 为数组。
value 优先于 defaultValue；合法 replace 重新初始化选择。禁用或断连时不可操作。

组件 size 统一推荐 small/medium/large，兼容 sm/md/lg；Switch/Card 只有 small/medium，
Avatar 使用 `src` 指定图片地址，额外兼容 default 和像素数值；Icon 同样使用 small/medium/large。
Text 样式对齐 Ant Design `Typography.Text`（`type`、`strong`、`italic`、`underline`、`delete`、`code`、`mark`、`keyboard`、`disabled`、`ellipsis`）；Button 样式对齐 Ant Design `Button`（`type`、`color`、`variant`、`shape`、`size`、`danger`、`ghost`、`disabled`、`block`、`loading`、`href`、`target`、`icon`、`iconPlacement`、`aria-label`）：`color` 含 13 个预设色，`loading` 支持布尔或 `{delay}`，`href` 走 HTTP(S)/相对地址校验，`target` 需与 `href` 同时声明。纯图标按钮要求非空 `aria-label`。
Avatar 可通过 props.children 声明文本，通过 style.backgroundColor/color/fontSize 声明原生样式；颜色接受 3/4/6/8 位十六进制形式，fontSize 为 1–4096 数值像素，gap 为 0–4096。图像优先与图像失败后的文本显示由 AntD 处理；允许空头像，不接受 name，宿主不生成首字母或配色。
Dropdown 必须选择 icon 或非空 children 作为触发区，不能同时提供；图标入口仍要求非空 ariaLabel。
application.menu.main 接受普通 Box/Card 组件树，页面无需 Iframe；Panel/Iframe 的专用根限制保持原样。

树选择可以分别绑定选中、整体值变化和清空：

```python
RenderNode(
    "TreeSelect",
    {"treeData": [{"value": "root", "title": "全部", "children": [{"value": "bj", "title": "北京"}]}],
     "allowClear": True},
    events={
        "ui.select": {"id": "node.choose"},
        "ui.change": {"id": "node.change"},
        "ui.clear": {"id": "node.clear"},
    },
)
```

选中节点发送该节点 `{value}`；change 发送整体选择值，单选为字符串，multiple 或 treeCheckable 为数组，
清空后分别为 null 和空数组；清空动作另外发送 `{}`。三项可按需省略绑定。
treeData 的 key 缺省取 value，搜索按 title 过滤；禁用或断连时不可操作。

滑块在拖动或键盘调整时为 `ui.change` 发送数值 `{value}`：

```python
RenderNode(
    "Slider",
    {"value": 20, "min": 0, "max": 100, "step": 1, "ariaLabel": "强度"},
    events={"ui.change": {"id": "level.change"}},
)
```

每次有效变化独立发送，不合并中间值；未绑定时仍可本地调整。
数值需落在 min/max 与 step 允许的取值上，禁用或断连时不可操作。

页签容器在切换时为 `ui.change` 发送当前激活页的 `{activeKey}`：

```python
RenderNode(
    "Tabs",
    {"items": [
        {"key": "one", "label": "概览", "children": [RenderNode("Text", {"value": "第一页"})]},
        {"key": "two", "label": "详情", "children": [RenderNode("Text", {"value": "第二页"})]},
    ]},
    events={"ui.change": {"id": "tab.change"}},
)
```

隐藏页内容不派发事件，默认保留其本地状态；destroyOnHidden 为真时切回后重新初始化。
禁用页签与断连时不可切换，页签内容同样计入层级与节点预算。

旧 action 组件与旧事件名（`ui.press`、`ui.toggle`、`ui.input`）已移除：节点只接受目录内的组件，
交互只通过 `events` 声明 `ui.click`、`ui.change`、`ui.press_enter`、`ui.select`、`ui.search`、`ui.clear`。
Handler 从 `payload["data"]` 读取事件数据；单个节点不再支持 action 声明。
下行帧的 schema_version 为 1。

`application.entry` 为应用范围，以当前 Web 连接为身份，不要求会话。首次访问时挂载，ready 被接受后保存浏览器访问标记，后续刷新跳过；clear 或失败关闭入口。`application.sidebar.footer`、`task.function` 与 `task.function.main` 同样使用应用范围；任务入口和页面在空对话中可用，切换会话保持实例。其余槽位默认使用真实会话，设置槽位也允许显式应用范围。文件组件与请求校验、回执见
[F_07](../docs/features/F_07_ui-contributions.md)。
