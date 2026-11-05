# 浏览器客户端能力接入规约

最近修订：2026-11-06。状态：已实现通用接入。
关联规约：[Gateway 运行时](S_01_gateway-extension-runtime.md)、[渲染服务](S_02_ui-render.md)。

## 范围

Gateway 包可声明 `ui.items` 及 `client.requestBindings`。两类能力分开判定：

- **应用级 UI 包**：至少一项 `web.slot.contribution` 指向 Gateway 应用级槽位（`application.entry`、
  `application.sidebar.footer`、`application.background`）的活动包。取得激活代次、所声明槽位的 `ui.ready` 与交互分派、`ops.ui.asset_url`
  及本包素材，并按激活代次拦截迟到调用。不需要声明 `client`。
- **客户端能力包**：声明 `client` 的应用级 UI 包。安装并激活后在上述能力之外直接具备请求绑定、
  `client.declarations` 与 `ops.client.*`（浏览器存储写删与页面刷新）；无需额外权限声明、Gateway 授权配置或确认，
  操作仍须通过包、连接、激活代次及参数校验。

没有声明应用级槽位的包保持既有行为。不会把 client 声明解释为认证规则。
请求触发点限于主 WebSocket RPC 和 Gateway HTTP，辅助 WebSocket 通道不在本地接入范围内。

浏览器声明只携带活动包身份、激活代次和请求绑定，不包含额外授权字段。Gateway 与前端同步使用此协议。

## 请求分派

所有主 WebSocket 请求先触发 `gateway.request`，包括 `webui.dispatch`。USER 插件先处理请求策略：
返回 `deny` 或 `consume` 时按原路由契约应答；调用 `next_handler(event)` 时，客户端能力包的
entry/footer 交互进入 BUILTIN 浏览器分派器，再触发对应 `ui.*` 事件并返回通用回执。
不属于本 Gateway 应用级 UI 包的目标继续已有请求链。

匿名交互是否允许由插件决定。宿主不根据字段名、按钮标识或页面内容识别登录行为。
鉴权职责由 [运行时规约](S_01_gateway-extension-runtime.md#6-扩展鉴权职责) 约束，由 USER 请求策略负责；CORE 默认行为和 WebSocket 异常处理策略保持原有契约。

HTTP 请求同样进入 `gateway.request`。合法的 `forward` 结果继续原接口，包含 CORE 默认返回的
`handled=false, route=forward`；插件无须接管每条请求。显式 `deny` 和 `consume` 保留插件决定。
素材路径 `/extension-assets/` 同样先经 `gateway.request`；策略继续后由激活代次与路径校验决定能否读取。

## 协议

- 连接建立、首次请求及包激活信息变化时下发 `client.declarations`。字段复用前端 ClientDeclarationSnapshot。
- `client.operation` 保留用于原请求结束等待的 `invocation_id`；不生成没有回执、去重或关联消费者的逐操作标识。
- `request_context` 作为请求扩展区独立传递给 gateway.request，不合并进业务 params。
- HTTP 只提供真实 header 和路径给事件；请求的判定属于插件。
- `webui.dispatch` 在 USER 请求策略继续之后，只分派本 Gateway 应用级 UI 包声明的 entry/footer；其他目标继续原有路径。
- ready 按声明贡献分派；交互事件必须匹配当前连接、包、槽位、实例、revision、node_key、action_id 和 args。
- 本地 UI 子集为 Box、Text、Button、Input、Form、Form.Item、Dropdown、Avatar、Card、Image，以及只用于 `application.background` 的 MediaSurface（[S_07](S_07_media-surface.md)）。
  Gateway Dropdown 仅支持一个展示子节点作为触发器，不开放 icon 属性；登录示例使用 Avatar。
  渲染复用正式 GatewayWebUIRuntime 的组件规则和预算，交互从同一 ActiveRenderRegistry 读取树并校验。
- localStorage 命令与前端 ClientOperation 一致，归属取当前 Ops 的包和连接，不接受调用者指定其他浏览器。
  没有执行回执、查询、重试或 token 有效期语义。
- SDK 适配器 `with_client_ops` 通过组合公开 Ops 提供 `ops.client.local_storage.set/delete` 与 `ops.client.page.reload()`。
- page.reload 无需单独授权，参数为空；插件显式决定何时刷新，存储变化不自动重建业务页面。
- 素材地址为 `/extension-assets/<package_id>/<activation_generation>/<path>`，由 `ops.ui.asset_url(path)` 生成；
  只读暴露应用级 UI 包当前激活下的 assets 目录，路径须位于该目录内的普通文件，包失活或重载后旧地址不可读取。
  支持 `Range`。素材响应固定包含 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff` 与
  `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads`，
  沙箱指令与前端 Iframe 一致且不含 `allow-same-origin`，直接打开的 HTML 同样以不透明来源运行。
- `/extension-assets/{package_id}/{generation}/{path}` 与 AgentServer 文件路由自身的成功及错误响应返回
  `Access-Control-Allow-Origin: *`，支持不透明源沙箱中的 ES module 和相对 JSON 请求；不开放凭据跨源读取。
  包激活、路径和 URL 授权校验保持有效；`gateway.request` 的提前拒绝、接管及失败响应保持既有行为，不全局添加 CORS。
- `ops.ui.asset_url(path, source="data")` 生成 `/extension-assets/<package_id>/<activation_generation>.data/<path>`，
  指向包数据目录（[S_01 第 3.1 节](S_01_gateway-extension-runtime.md)）中 `public/` 内的普通文件；
  `source` 只接受 `package`（默认）与 `data`。代次、越界与响应头规则同包内素材。
- `ops.appearance.apply(appearance)` 供应用级 UI 包为当前连接提交或撤销受控外观，接口与生命周期见 [S_08](S_08_appearance.md)。
- Web UI 静态服务将 `/extension-assets/` 请求按原路径转发到 WebChannel HTTP 服务，与 Vite 开发服务保持一致。
  GET/HEAD 保留后端状态码、内容类型和缓存策略；无效或过期素材返回后端的 404，不返回 SPA HTML。
  打包版前端由 `channels/web/app_web.py` 将 `/extension-assets/` 与 `/api` 一同转发到 WebChannel，响应按块写回。

## 不变量

1. 宿主不解释 auth、token、username、logout 等业务字段。
2. 包与连接身份来自宿主上下文；客户端目标只能选择已经由该连接收到的实例。
3. localStorage 写删仅向发起调用的浏览器投递，Gateway 不保存浏览器数据副本。
4. 无包时不改变原请求；请求路由及异常语义沿用已合入的 Gateway 逻辑。
5. 断线清理实例和连接映射；包失活拒绝旧动作与旧资源。重载后实例 revision 不复用，旧激活中延迟的 Ops 不得投递到新激活。
6. 活动客户端包的浏览器能力不依赖额外授权；安装、重载和重启统一使用本条规则。
   请求绑定、`client.declarations` 与 `ops.client.*` 只属于声明 `client` 的包；仅声明槽位的包调用 `ops.client.*` 被拒绝。
7. 素材请求与其他 HTTP 请求一样先经 `gateway.request`，插件拒绝与接管保持有效；策略继续后由平台判定包、激活代次与路径。

## 验证入口

定向用例位于 `tests/unit_tests/gateway/extensions_v2/test_gateway_browser_bridge.py`；
应用级 UI 包与素材路由见 `test_gateway_application_assets.py`，`app_web` 转发与流式输出见
`tests/unit_tests/channels/web/test_app_web_extension_assets.py`。用例使用通用组件和策略，独立于插件示例及其素材。
设计与验证记录见 [F_08](../features/F_08_application-packages-and-assets.md)。

client.declarations 仅约束参与浏览器能力协议的 Gateway 包的命令、请求绑定和界面激活状态，不作为所有插件文件的准入表。插件文件 URL 不根据该表判断可读性；文件所属后端执行路径、归属和激活校验。前端按界面实例卸载取消请求并释放 Blob，携带凭据的普通接口请求继续跟随请求上下文。

前端规范化保留 Gateway 客户端帧的 activation_generation，界面实例随所属激活失效而卸载；资源请求跟随界面卸载取消，不通过解析文件 URL 查包清单。普通 AgentServer 界面帧不因 Gateway 客户端声明错误而被拒绝。

普通 UI 包不加入 client.declarations。仅客户端能力包的帧携带 activation_generation；失活调用不得通过移除标记重新进入普通 UI 通路。

## English contract summary

Every main WebSocket request enters gateway.request before application UI dispatch. USER plugins decide whether
to deny, consume, or continue a request. Continued entry/footer interactions for participating packages are handled
by the BUILTIN browser dispatcher. Anonymous interaction policy belongs to plugins; the host does not recognize
login fields or action identifiers. Other targets retain the existing request chain and authentication behavior.
HTTP forward results continue to the native route even when handled is false. Explicit plugin denial and consumption
remain effective. Generic regression tests do not depend on an example package.
Any package contributing to a Gateway application slot participates in application UI (generation, ready and
interaction dispatch, asset URLs) without declaring client; request bindings, client declarations and client
operations remain limited to packages declaring client. Package asset requests pass through gateway.request like other
HTTP requests and are served with no-store, nosniff and a sandbox Content-Security-Policy, and forwarded and streamed
by the Web UI server.
Gateway Dropdown accepts one display child as its trigger; icon triggers remain outside this Gateway integration.
Active installed client packages can write/delete their own storage, bind requests and reload the current page without
separate permission declarations or deployment configuration. Package identity, connection ownership, activation
generation and input validation still apply. Gateway and frontend use the same declaration and operation protocol.
Declarations contain no grant fields. Operations retain invocation_id for request completion coordination and do not
carry unused operation identifiers.
The built frontend server forwards `/extension-assets/` to WebChannel HTTP, preserving GET/HEAD responses and backend
errors instead of serving the SPA entry page. Asset identity and activation checks remain owned by the Gateway.
Successful and error responses produced by the two file routes include `Access-Control-Allow-Origin: *`
for opaque-origin sandbox modules and JSON fetches, without credentialed cross-origin access. This does not
alter resource authorization or add CORS to ordinary routes or early policy denial, consumption and failure responses.
