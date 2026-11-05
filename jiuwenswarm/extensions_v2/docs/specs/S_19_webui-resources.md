# AgentServer WebUI 文件资源

最近修订：2026-11-05。组件字段以 [S_17](S_17_webui-component-props.md) 为准。
本文件是 AgentServer 文件资源 API 的契约。

## API 与职责

AgentServer 插件负责生成和写入页面文件；宿主负责提供受管目录、登记文件、返回 URL、校验读取和清理。Gateway 保留已有的静态 assets 接口，本次不增加 Gateway 输出目录能力。

`await ops.ui.create_resource_dir(*, previous: str = "") -> WebUIResourceDirectory`
创建当前插件及会话/应用连接专用的临时目录，返回 `resource_id: str` 和 `path: Path`。
插件必须在 `async with directory:` 中完成文件写入；退出写入上下文后才能发布。阻塞写入放在线程中，插件收到取消时须等待写入线程结束再退出上下文。
线程写入使用公共方法 `jiuwenswarm.extensions_v2.webui.resources.complete_before_cancel(operation)`：操作只启动一次，即使调用方连续取消，也等待实际操作结束，再传播第一次取消。
未取消时返回结果或原样抛出操作异常；取消期间操作失败时仍抛出 `CancelledError`，并将操作异常保留为 `__cause__`。操作自身取消是终止状态，不继续等待。
宿主在作用域关闭时先撤销地址，再等待已开始的写入结束，最后删除目录。`previous` 仅用于保留同包同作用域已有资源的用户路由，不释放旧目录。

`await ops.ui.asset_url(path: str, *, resource_id: str = "") -> str`
无 resource_id 时，读取当前插件安装包的 assets 目录中已有的静态文件，返回 HTTP URL。
有 resource_id 时，登记该输出目录中的完整文件集并返回指定入口的 HTTP URL。同一目录下的中英 HTML、CSS 和相对引用共享 URL 根目录。首次登记检查全部文件并记录摘要，后续只返回已登记路径；发布后不能再次写入或覆盖。
静态文件和输出目录的 URL 都受包激活及路由归属约束；静态文件不会因资源释放而从安装包删除。

`await ops.ui.release_resources(resource_id: str) -> None`
撤销资源地址并释放受管输出目录；已成功释放的 ID 再次释放无副作用。归属不符拒绝释放。异常/取消时插件释放新目录，成功投递新帧后才释放旧目录。
插件处理取消时若清理也失败，继续传播原取消，并以异常原因保留清理失败；已有写入失败时，两项失败共同保存在原因异常组中。
原接收文件内容的发布接口从本地实现、调用方和示例中移除，不保留兼容入口。

路径使用 ASCII 字母、数字、点、下划线、连字符和斜杠，必须为规范相对路径，拒绝绝对路径、穿越、控制字符、反斜杠、URL 转义、Windows 保留名、尾随点、符号链接及仅大小写不同的重复文件。
允许的扩展名与 MIME 在 common/webui_resources.py 维护。每文件上限 1 MiB；每个生成目录最多 64 文件、4 MiB；同一包激活及作用域最多 32 个目录（含未发布目录）。静态文件按请求校验路径、文件类型和大小，不复制到临时目录。
无文件、预算、路径或状态错误抛 ValueError；无效归属、未激活包或关闭作用域抛 PermissionError；未取消时 I/O 错误原样抛出，取消时按上述异常规则处理。

## URL 与访问

资源使用 `/extension-assets/_agent/{route}/{resource_id}/{path}`。`_agent` 为保留入口，不属于 Gateway 插件包目录。
route 是规范 URL-safe 编码的 `[user_id, session_id, connection_id]`，其中会话和连接只能有一个非空；渠道固定 web。
该字段只用于沿现有 E2A 机制选择 AgentServer。resource_id 是 256 位随机令牌，URL 持有者可读取该文件集，浏览器无需注入认证请求头。
相对文件引用保留相同令牌和路由；URL 不是任意磁盘路径，也不允许访问其他资源集或未发布文件。
Iframe 中的资源地址由宿主依据配置的 Gateway API 基址解析，并保留基址路径前缀；同源且无显式基址时保留原相对地址。Electron FrontendOnly 使用桌面注入的 HTTP API 基址，因此 HTML 及相对 CSS 请求均进入 Gateway，而非 file:// 本地目录。

Gateway HTTP 路由通过内部 `webui.resource.read` 请求读取文件；此请求不登记为浏览器 WS 方法。
参数为 `{resource_id, path, connection_id}`，请求信封携带 user_id、session_id、channel_id。
AgentServer 校验全部路由归属、资源令牌、包激活代次和文件摘要，返回 `{data: Base64, media_type: string}`。
Base64 仅用于现有 JSON 内部传输；WebUI 帧只含 URL，HTTP 返回解码后的字节。Gateway 校验大小、编码与 MIME。
未知/撤销资源返回 404，AgentServer 不可用返回 503，非法内部响应返回 502。GET、HEAD 均返回准确 MIME、no-store、nosniff、no-referrer。
AgentServer 文件路由和 Gateway 包静态素材路由自身的成功、错误响应均返回 `Access-Control-Allow-Origin: *`，允许不透明源（`Origin: null`）的沙箱页面加载 ES module 及相对 JSON 文件。不返回 `Access-Control-Allow-Credentials`；带凭据的跨源读取不属于该能力。
该响应头不替代 URL 令牌、归属和包激活校验，也不改变 `gateway.request` 的策略顺序。策略提前拒绝、接管或失败的响应不由文件路由添加 CORS；普通接口保持既有行为。
HTTP 对 HTML 等活动内容施加与 Iframe 相同的 CSP sandbox，不包含 allow-same-origin；直接打开资源亦不取得宿主 DOM/存储权限。

## 生命周期与不变量

- R-01：创建/发布时绑定可信 Ops 的包身份和当时激活代次，以及真实会话或当前 WebUI 连接；不接受浏览器提供的包磁盘根目录。
- R-02：新集独立于旧集；插件在新帧成功投递后释放旧集，失败或取消时释放新集。
- R-03：包撤销、会话关闭、应用连接关闭及宿主停机清理所属资源；session 资源不因单个浏览器连接断开而删除。
- R-04：关闭的会话/连接不能再发布；旧代次 URL 不会在包重新加载后恢复有效；服务重启不恢复临时资源 URL。
- R-05：用户路由不同或作用域不同不能借 previous 更新/释放资源；route 单独不授予访问权。
- R-06：不扩大 srcDoc/帧预算，不裁剪 HTML，不以 file:// 或 Blob URL 代替资源链路。
- R-07：释放、作用域关闭及停机从清理入口开始保护取消，包括等候资源锁、写入结束和实际目录删除；连续取消不打断已接受的操作。关闭服务还须完成临时根目录的清理及句柄释放。删除失败时保留已撤销登记以供后续显式清理，旧地址不可读取，不自动重试 I/O。

验证入口：`test_resources.py`、`tests/integration/test_webui_resource_browser.py`、共享 `webuiPanelDocumentCases.json` 和前端组件/客户端测试。
