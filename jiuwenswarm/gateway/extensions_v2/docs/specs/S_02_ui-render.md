# Gateway 渲染服务规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_02 / ui-render` |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/webui/`、`event_types/webui.py`、`handler_ops/ui.py` |
| 设计依据 | [详细架构第 6 节](../designs/extensions-v2-architecture.md)、AgentServer [F_06](../../../../extensions_v2/docs/features/F_06_render-service.md) |
| 最近一次修订日期 | 2026-11-05 |
| 契约状态 | 已实现；本文是 Gateway 侧 `ui.render` 与 Ops 契约的唯一维护位置 |

本文维护 Gateway 侧渲染的对外契约：事件与 Ops 形状、槽位表、组件目录、下行帧、尺寸上限与错误码。平台不预置任何视图：`ui.render` 只负责校验并投递扩展包提交的树。与 AgentServer 同名的模型保持字段一致；差异只在第 6 节列出的必要项。

## 1. 事件契约

| 项 | 内容 |
| --- | --- |
| 事件名 | `ui.render` |
| 输入 | `UiRenderInput(target, root=None, mode=RenderMode.REPLACE, *, visible_when=None)` |
| 输出 | `UiRenderOutput(status, revision=0)` |
| CORE Handler | `webui/core_handlers/render_core.py::build_render_core_handler(transport, registry)` |
| 匹配 | `match_pattern_subset` |

`status` 取 `delivered`、`rejected`、`delivery_failed`。`revision` 是本次提交后的实例版本；被拒绝时为 0 或上次版本。

可选实例显示条件 visible_when 的形态、快照和替换语义统一遵循 [S_17 §2.4](../../../../extensions_v2/docs/specs/S_17_webui-component-props.md#24-渲染实例的显示条件)，由客户端槽位宿主管理外层容器，不属于组件 props。

## 2. Ops 契约

| 项 | 内容 |
| --- | --- |
| 命名空间 | `ui` |
| 方法 | `ui.render(request: UiRenderInput) -> UiRenderOutput` |
| 装配 | `ops.build` BUILTIN handler 调用 `add_namespace("ui")` 与 `add_method("ui", "render", ...)` |
| 语义 | 开始一条新的 `ui.render` 事件链，不推进当前链 |

`ui.render` 事件链与调用方所在的 `gateway.request` 链相互独立；调用方的返回值不受渲染结果影响。

## 3. 槽位契约

Gateway 只接受自己能按 WebSocket 寻址的槽位：

| 槽位 | 范围 | 容量 | 说明 |
| --- | --- | --- | --- |
| `application.entry` | `application` | `single` | 首屏整页宿主槽位，前端常驻挂载 |
| `application.sidebar.footer` | `application` | `list` | 侧边栏底部列表槽位，可同时容纳多个包的内容 |
| `application.background` | `application` | `single` | 全窗口底层背景，只接受 `MediaSurface` 根；宿主策略与消息协议见 [S_07](S_07_media-surface.md) |
| `application.splash` | `application` | `single` | 启动开场，只接受 `MediaSurface` 根；声明、时机与平台帧见 [S_09](S_09_startup-splash.md) |
| `application.settings` | `application` | `list` | 与 AgentServer 共用的设置槽位；Gateway 只渲染其包按 S_18 声明的 application 范围配置项，接受普通组件树；见 [S_10](S_10_application-settings.md) |

槽位是通用能力：任何 Gateway 包都可以渲染这些槽位，平台不规定其内容。未知槽位返回 `SCHEMA_ERROR`。会话槽位由 AgentServer 渲染，Gateway 侧一律拒绝。

## 4. 目标与实例键

| 槽位范围 | 实例键 |
| --- | --- |
| `session` | `session_id\|slot_id\|package_id\|instance_key` |
| `application` | `application\|connection_id\|slot_id\|package_id\|instance_key` |

- `application` 目标不得携带 `session_id`；缺少 `connection_id` 返回 `CONNECTION_REQUIRED`。
- `connection_id` 由 Gateway 从实际 WebSocket 注入，前端不下发；前端携带且与调用方不一致时拒绝。
- 下行 `target` 不包含 `connection_id`，`application` 目标也不包含 `session_id`。
- `package_id`、`instance_key`、`connection_id` 各限 200 字节；字段不得含 `|`，不得含首尾空白。

## 5. 上限与下行帧

| 限制 | 值 |
| --- | --- |
| 树深度 | 8 |
| 节点数 | 512 |
| 单文本 prop | 16 KiB |
| 列表项 | 100 |
| 整帧 | 256 KiB |
| 活跃实例 | 每个连接最多 32 个 |
| 事件声明 `args` | 8 KiB |

下行事件由 `GatewayTransport` 经 `WebChannel.send_event` 直接发到目标 socket：

| 事件 | 载荷 |
| --- | --- |
| `webui.render` | `target`、`revision`、`mode`、`root`、`schema_version=1`、可选 `visible_when` |
| `webui.render.clear` | `target`、`revision`、可选 `reason` |
| `webui.render.error` | `target`、`revision`、`code`、`message` |

投递返回 `True` 只表示帧已交给渠道，不代表浏览器已应用。

## 6. 与 AgentServer 的必要差异

组件属性、嵌套字段、children 和事件数据统一遵循
[S_17 WebUI 组件 Props 权威规约](../../../../extensions_v2/docs/specs/S_17_webui-component-props.md)。
本节只说明宿主范围，不独立定义另一套 props。

| 差异 | 原因 |
| --- | --- |
| 槽位表只含两个 application 槽位 | Gateway 不渲染会话槽位，避免与 AgentServer 双写同一实例 |
| 传输层按 `ws_id` 直接 `send_event` | Gateway 拥有 socket，不经过 AgentServer 推送通道 |
| 连接事实来自 `SessionContext.metadata` | Gateway 没有 AgentServer 的 contextvar 调用上下文 |
| `Form` / `Form.Item`、`Button.form`、布局与富菜单扩展 | Gateway 页面支持表单和富菜单；AgentServer 保持其组件子集；`Input` 不带 `name` |
| 无 `contributions.py` 与目录服务 | 槽位固定，扩展包不能新增槽位 |

表单字段、名称限制和规则见 S_17。点击带 form 引用的按钮时，前端先校验表单，再组装字段数据；普通按钮发送空 data。
共同组件使用新属性：Button.value、Text.type / strong / code / ellipsis、Avatar.src，不接受旧字段别名。

Gateway 的组件规则采用浏览器组件目录的子集；运行时与 AgentServer 分别持有实例。通用模型保持对应，浏览器能力的激活归属及版本生命周期由 [S_06](S_06_local-browser-bridge.md) 维护，不跨宿主 import。

Gateway 不支持独立 Icon 或 Dropdown.icon；Button.icon 使用共享白名单，由生成脚本在本域生成 Python 文件。
Gateway 的 Dropdown 使用一个展示子树作为触发器，菜单 label 继续有效。表单、菜单和布局的共享验证向量位于前端 tests/fixtures/webuiGatewayCases.json。

Component props follow S_17, including host-specific capabilities. Gateway keeps forms and rich menus, accepts the current Button/Text/Avatar props, and rejects legacy aliases. Button icons use the shared whitelist; standalone Icon and Dropdown.icon remain unsupported in Gateway.

## 7. 错误码

| 码 | 产生位置 | 含义 |
| --- | --- | --- |
| `SCHEMA_ERROR` | `validate_request` / `frame_payload_bytes` | 槽位、组件、props、事件、尺寸不合法 |
| `CONNECTION_REQUIRED` | `validate_target` | application 目标缺少连接 |
| `DELIVERY_FAILED` | 传输层返回失败 | 帧未交给渠道，实例状态已回滚 |

## 8. 不变量

1. revision 按实例单调递增，不跨实例比较。
2. 先校验整帧字节量，再提交 revision；失败不产生 revision 空跳。
3. 投递失败恢复旧 revision 与旧树；新建实例直接移除。
4. 容量淘汰在新帧投递成功后执行，被淘汰实例收到 `webui.render.clear`。
5. `mode=clear` 提交 revision 后下发清除帧并移除实例。
6. 连接断开时清除该连接的 application 实例。

## 9. 验证索引

Text.value、Button.value 和 Switch.ariaLabel 的多语言文本值与前端组件 C-33 对齐：接受普通字符串或完整 zh/en 字符串对象，逐字段文本预算不变；缺失语言、额外字段或非法类型拒绝。语言由前端当前界面语言决定，Gateway 不替换插件文案或在服务端保存客户端语言。

| 契约 | 用例 |
| --- | --- |
| `ops.ui.render` 下发与 revision | `test_gateway_ui_render.py` |
| 未知组件与未知槽位拒绝 | `test_gateway_ui_render.py` |
| clear 模式 | `test_gateway_ui_render.py` |
| 目标连接隔离与断线清理 | `test_gateway_ui_render.py` |
| 组件目录覆盖 `Form` / `Form.Item` / `Button.form` / `Dropdown` | `test_gateway_ui_render.py` |

## 内联文档与面板打开

Iframe 必须且只能提供 src 或 srcDoc；srcDoc 和 title 支持完整 zh/en 文本值，每个标题仍限 200 UTF-8 字节、每种语言的文档限 16 KiB。固定沙箱不变。Button.openPanel 只表达同包同会话右侧目标，ui.click 明确成功后由当前客户端导航，普通数据帧不抢焦点、不重开已关闭页签。属性主责见 AgentServer 文档目录的 S_17_webui-component-props。

Iframe accepts exactly one of src or srcDoc. Localized documents and titles retain the shared text limits and fixed sandbox. The plugin declares minWidth in pixels (default 0); the host supplies horizontal scrolling for narrower containers, with the same rule for URL and inline documents and no width mode selector. Button.openPanel opens a rendered same-package, same-session panel only after an accepted explicit click. Render updates do not navigate or affect other clients.
