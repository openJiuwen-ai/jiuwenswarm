# Gateway 渲染服务规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_02 / ui-render` |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/webui/`、`event_types/webui.py`、`handler_ops/ui.py` |
| 设计依据 | [详细架构第 6 节](../designs/extensions-v2-architecture.md)、AgentServer [F_06](../../../../extensions_v2/docs/features/F_06_render-service.md) |
| 最近一次修订日期 | 2026-11-03 |
| 契约状态 | 已实现；本文是 Gateway 侧 `ui.render` 与 Ops 契约的唯一维护位置 |

本文维护 Gateway 侧渲染的对外契约：事件与 Ops 形状、槽位表、组件目录、下行帧、尺寸上限与错误码。平台不预置任何视图：`ui.render` 只负责校验并投递扩展包提交的树。与 AgentServer 同名的模型保持字段一致；差异只在第 6 节列出的必要项。

## 1. 事件契约

| 项 | 内容 |
| --- | --- |
| 事件名 | `ui.render` |
| 输入 | `UiRenderInput(target, root=None, mode=RenderMode.REPLACE)` |
| 输出 | `UiRenderOutput(status, revision=0)` |
| CORE Handler | `webui/core_handlers/render_core.py::build_render_core_handler(transport, registry)` |
| 匹配 | `match_pattern_subset` |

`status` 取 `delivered`、`rejected`、`delivery_failed`。`revision` 是本次提交后的实例版本；被拒绝时为 0 或上次版本。

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

槽位是通用能力：任何 Gateway 包都可以渲染这两个槽位，平台不规定其内容。未知槽位返回 `SCHEMA_ERROR`。会话槽位由 AgentServer 渲染，Gateway 侧一律拒绝。

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
| `webui.render` | `target`、`revision`、`mode`、`root`、`schema_version=1` |
| `webui.render.clear` | `target`、`revision`、可选 `reason` |
| `webui.render.error` | `target`、`revision`、`code`、`message` |

投递返回 `True` 只表示帧已交给渠道，不代表浏览器已应用。

## 6. 与 AgentServer 的必要差异

| 差异 | 原因 |
| --- | --- |
| 槽位表只含两个 application 槽位 | Gateway 不渲染会话槽位，避免与 AgentServer 双写同一实例 |
| 传输层按 `ws_id` 直接 `send_event` | Gateway 拥有 socket，不经过 AgentServer 推送通道 |
| 连接事实来自 `SessionContext.metadata` | Gateway 没有 AgentServer 的 contextvar 调用上下文 |
| 组件目录补 `Form` / `FormItem` / `Dropdown`，`Button` 增加 `form` | 浏览器组件契约要求表单用 `Form.name` + `FormItem.name` 描述，按钮通过 `form` 引用同树 Form；`Input` 不带 `name` |
| 无 `contributions.py` 与目录服务 | 槽位固定，扩展包不能新增槽位 |

表单组件契约：`Form` 必须有唯一 `name`，子节点只能是 `FormItem`；`FormItem` 必须有 `name`，子节点只能是 `Input`，可带 `rules`（`required` / `min` / `max` / `pattern` / `message`）；`Button.form` 必须引用同一棵树中的 `Form.name`。点击时前端按 `Form.name` 组装 `ui.click.data`。AgentServer 副本暂不引入这三条规则，两份 `component_rules.py` 在表单契约上有意分歧。

除上表外，`webui/models.py`、`schema.py`、`registry.py`、`component_rules.py` 与 AgentServer 保持同构，修改时两侧同步。

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

| 契约 | 用例 |
| --- | --- |
| `ops.ui.render` 下发与 revision | `test_gateway_ui_render.py` |
| 未知组件与未知槽位拒绝 | `test_gateway_ui_render.py` |
| clear 模式 | `test_gateway_ui_render.py` |
| 目标连接隔离与断线清理 | `test_gateway_ui_render.py` |
| 组件目录覆盖 `Form` / `FormItem` / `Button.form` / `Dropdown` | `test_gateway_ui_render.py` |
