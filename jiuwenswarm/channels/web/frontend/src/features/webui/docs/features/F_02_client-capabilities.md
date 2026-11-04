# Web 浏览器能力与组件扩展实现记录

建档日期：2026-11-03。最近修订：2026-11-04。

关联规约：[客户端能力](../specs/S_02_client-capabilities.md)、[页面布局](../specs/S_03_page-composition.md)。

## 范围与职责

前端交付 localStorage 命令执行、按传输类型的请求绑定、请求上下文生命周期、Form/Form.Item、Dropdown、
Box 页面布局、受保护资源读取、entry 全视口重入和 sidebar.footer。
Gateway 接入与登录示例分别提交，配套契约见 [Gateway 浏览器桥接](../../../../../../../../gateway/extensions_v2/docs/specs/S_06_local-browser-bridge.md)和[示例联调](../../../../../../../../gateway/extensions_v2/docs/features/F_07_local-login-integration.md)。
宿主不解释 auth、accessToken、username 或 logout 等业务字段。

宿主负责插槽位置及基础留白，包括 sidebar.footer 的底部 12px；页面尺寸、间距、图片、背景和菜单位置由插件声明。
表单字段、规则、双语文案与业务动作属于插件。内置控件和宿主错误提示跟随前端中英文资源。

## 采用方案

受信任的 Gateway 下发 client.declarations，WebUI 传输适配器把快照交给 browserClientCapabilities。
声明不是全体 WebUI 插件目录，普通 UI 包保持原路径；客户端能力包的帧额外校验 activation_generation。
包存储按稳定 Gateway ID、packageId 与逻辑 key 隔离。安装并激活的客户端包可直接写删存储、绑定请求及刷新页面，
无需额外授权；声明和命令中没有额外权限字段或无消费者的逐操作标识。

主 WebClient 与独立 /ws/git 客户端按声明写入 request_context，HTTP 调用使用 gatewayFetch。
实际 Gateway HTTP 读取、上传和下载接入同一传输；Designer 本地 blob: 素材继续使用原生 fetch。
辅助 /ws/git 的 Gateway 策略接入尚未完成，前端发送声明字段不代表该通道已执行插件策略。

绑定值变化时取消旧请求和响应流，已有请求 ID 与本地 epoch 隔离迟到结果。
新上下文就绪后重新读取配置、模型目录及当前会话尚未完成的元数据和历史；已加载的业务 Store 保留。
元数据只有明确返回 NOT_FOUND 才能判定会话不存在。同一上下文内合并重复元数据请求，effect 退出时释放历史订阅。
独立推送的跨上下文归属仍需服务端生命周期协作，本地计数不提供该保证。

插件显式下发 page.reload 后才刷新整页。该命令检查连接、激活代次、参数及时限；
按消息顺序执行，立即调用 window.location.reload()，不等待原请求回包。
存储变化、声明注入、重连和其他标签页 storage 事件均不自动刷新，也不逐个清理业务 Store。

原生媒体元素不能附加绑定 header，因此受保护图片、视频、PDF 和下载先经 gatewayFetch 获取 Blob。
上下文变化或组件退出时终止旧请求并释放 Object URL。资源就绪前不挂载图片。
iframe 内部网页、HTML 相对资源、直接导航及桌面原生打开仍需独立资源授权接入。

Form 使用 AntD 本地状态与校验，Button.form 在实例内关联表单并通过 ui.click.data 发送字段。
隐藏页签及暂时停用保留草稿，断连和销毁清理字段。Form.Item 使用正式协议名，不接受旧 FormItem 名称。
Dropdown 只接受一个触发源：图标加可访问标签，或一棵展示子树。菜单标签允许展示节点、禁用项及分隔线，
拒绝交互子树及未显式关闭预览的 Image。Gateway 仅开放展示子树触发器；共享用例校验两端共同支持的结构，图标触发器由前端单独验证。

entry 使用原生 modal dialog 管理焦点与底层交互阻断，可在 clear 后重新打开；错误实例仍保留阻断。
非 entry 插槽在新上下文就绪后重新上报 ui.ready，恢复失效处理移除的贡献。

## 验证

Node 测试入口为 package.json 中 test:webui-*、test:app-request-context 和 test:web-client-runtime-ack。
共享组件契约向量位于 tests/fixtures/webuiGatewayCases.json，由前端协议管线与 Gateway schema 分别执行。
真实浏览器协议测试入口为 tests/webuiBrowserServer.mjs 与 tests/webuiBrowser.test.mjs；其 WebSocket 为协议桩，
不能替代真实 Gateway 或身份服务验收。

2026-11-04 精简前的回归结果：前端 418 项 Node 测试全部通过（contracts 119、components 117、protocol 14、runtime 17、
events 93、transport 7、renderer 17、client 22、App request context 4、WebClient runtime ACK 8）。
Gateway 移除图标触发能力后，共享用例保留 28 项共同契约；图标和别名仍在前端单独验证。重新执行 contracts 119 项和 components 115 项，全部通过。
Chrome 157.0.8103.38 协议浏览器测试通过，覆盖表单与键盘菜单、modal 焦点、clear 重入、跨标签页存储、
HTTP/WS 绑定、显式刷新、双语错误提示、宽窄布局与日志脱敏。Vite 生产构建通过。
手工核对本次样式引用的语义主题变量均已在主题中声明，未引入新产品颜色。

## 设计演进与边界

2026-11-03 的前端独立验证使用宿主适配接口及协议桩；当时的“后端未接入”是该阶段状态。
2026-11-04 交付补齐 Gateway 声明投递与客户端操作，登录示例独立成包，不改变既有认证门禁策略。
早期按业务 Store 逐个重置的方案已改为插件显式 page.reload；不注入或要求业务事件回传 context_stamp。

Chrome/Chromium 107 是最低基线，本机使用的浏览器版本只证明对应环境；未验证的平台及版本不能扩大解释为通过。
本次遵守本地约束，不运行格式化、tsc 或静态检查；Vite 构建不代表类型检查通过。

## English summary

The frontend provides generic browser storage commands, transport-specific request bindings, explicit page reload,
local forms, menu composition, protected resource loading and application slots. Gateway integration and the login
example are separate commits. The host does not interpret authentication fields or reset business stores individually.
Context changes cancel old requests and streams; ready contexts resume initialization and unfinished conversation reads.
Only explicit NOT_FOUND metadata errors establish a missing conversation. Independent pushes and embedded resource
navigation still require their owning server contracts. Shared vectors verify the common frontend/Gateway component subset;
browser protocol fixtures do not establish real identity-service acceptance. Authentication policy is unchanged.
