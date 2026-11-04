# 浏览器客户端能力接入规约

最近修订：2026-11-04。状态：已实现通用接入。
关联规约：[Gateway 运行时](S_01_gateway-extension-runtime.md)、[渲染服务](S_02_ui-render.md)。

## 范围

Gateway 包可声明 `ui.items` 及 `client.requestBindings`。
浏览器操作须由部署配置 `extensions.gateway.client_grants` 为包授权；包不能自行取得权限。
没有 client 声明的包保持既有行为。不会把 client 声明解释为认证规则。
请求触发点限于主 WebSocket RPC 和 Gateway HTTP，辅助 WebSocket 通道不在本地接入范围内。

## 请求分派

所有主 WebSocket 请求先触发 `gateway.request`，包括 `webui.dispatch`。USER 插件先处理请求策略：
返回 `deny` 或 `consume` 时按原路由契约应答；调用 `next_handler(event)` 时，客户端能力包的
entry/footer 交互进入 BUILTIN 浏览器分派器，再触发对应 `ui.*` 事件并返回通用回执。
不属于本 Gateway 客户端能力包的目标继续已有请求链。

匿名交互是否允许由插件决定。宿主不根据字段名、按钮标识或页面内容识别登录行为。
已合入的认证门禁、CORE 默认行为和 WebSocket 异常处理策略保持原有契约。

HTTP 请求同样进入 `gateway.request`。合法的 `forward` 结果继续原接口，包含 CORE 默认返回的
`handled=false, route=forward`；插件无须接管每条请求。显式 `deny` 和 `consume` 保留插件决定。

## 协议

- 连接建立、首次请求及包激活信息变化时下发 `client.declarations`。字段复用前端 ClientDeclarationSnapshot。
- `request_context` 作为请求扩展区独立传递给 gateway.request，不合并进业务 params。
- HTTP 只提供真实 header 和路径给事件；请求的判定属于插件。
- `webui.dispatch` 在 USER 请求策略继续之后，只分派本 Gateway 包声明的 entry/footer；其他目标继续原有路径。
- ready 按声明贡献分派；交互事件必须匹配当前连接、包、槽位、实例、revision、node_key、action_id 和 args。
- 本地 UI 子集为 Box、Text、Button、Input、Form、Form.Item、Dropdown、Avatar、Card、Image。
  渲染复用正式 GatewayWebUIRuntime 的组件规则和预算，交互从同一 ActiveRenderRegistry 读取树并校验。
- localStorage 命令与前端 ClientOperation 一致，归属取当前 Ops 的包和连接，不接受调用者指定其他浏览器。
  没有执行回执、查询、重试或 token 有效期语义。
- SDK 适配器 `with_client_ops` 通过组合公开 Ops 提供 `ops.client.local_storage.set/delete` 与 `ops.client.page.reload()`。
- page.reload 要求独立部署授权，参数为空；插件显式决定何时刷新，存储变化不自动重建业务页面。
- 素材只读暴露活动包 assets 目录，路径须位于该目录，包失活后不可读取。

## 不变量

1. 宿主不解释 auth、token、username、logout 等业务字段。
2. 包与连接身份来自宿主上下文；客户端目标只能选择已经由该连接收到的实例。
3. localStorage 写删仅向发起调用的浏览器投递，Gateway 不保存浏览器数据副本。
4. 无包时不改变原请求；请求路由及异常语义沿用已合入的 Gateway 逻辑。
5. 断线清理实例和连接映射；包失活拒绝旧动作与旧资源。重载后实例 revision 不复用，旧激活中延迟的 Ops 不得投递到新激活。

## 验证入口

定向用例位于 `tests/unit_tests/gateway/extensions_v2/test_gateway_browser_bridge.py`。
用例使用通用组件和策略，独立于插件示例及其素材。

普通 UI 包不加入 client.declarations。仅客户端能力包的帧携带 activation_generation；失活调用不得通过移除标记重新进入普通 UI 通路。

## English contract summary

Every main WebSocket request enters gateway.request before application UI dispatch. USER plugins decide whether
to deny, consume, or continue a request. Continued entry/footer interactions for participating packages are handled
by the BUILTIN browser dispatcher. Anonymous interaction policy belongs to plugins; the host does not recognize
login fields or action identifiers. Other targets retain the existing request chain and authentication behavior.
HTTP forward results continue to the native route even when handled is false. Explicit plugin denial and consumption
remain effective. Generic regression tests do not depend on an example package.
