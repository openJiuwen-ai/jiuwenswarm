# Gateway 扩展运行时规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_01 / gateway-extension-runtime` |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/` |
| 设计依据 | [详细架构](../designs/extensions-v2-architecture.md) |
| 最近一次修订日期 | 2026-11-07 |
| 契约状态 | 已实现；本文是 Gateway 扩展对外契约的唯一维护位置 |

本文维护 Gateway 扩展运行时的对外契约：入口函数、事件数据类、路由语义、错误码、配置键与不变量。具体实现方案与决策历史见对应 feature 文档。

## 1. 入口契约

| 入口 | 签名 | 语义 |
| --- | --- | --- |
| `init_gateway_extensions` | `async (channel, config=None) -> GatewayRuntime \| None` | 装配并启动；已装配时返回同一实例；禁用或失败返回 `None` |
| `intercept_gateway_frame` | `async (ws, req_id, method, params, headers=None, *, request_context=None) -> tuple[bool, dict]` | 返回 `(visited, params)`；`visited=True` 表示已处置，原生分发必须跳过 |
| `notify_gateway_disconnect` | `async (ws, session_ids=None) -> None` | 对齐 WebChannel 断连回调，向运行时传递会话集合并清理该连接的 connection 作用域状态；不抛异常 |

三个入口均幂等或 fail-open：任何异常都不得阻断 Gateway 的原生路径。

## 2. 配置契约

配置位置：`extensions.gateway`。

| 键 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | `false` 时整体不装配 |
| `install_dir` | str | `extension_pkgs` | 与 AgentServer 共享的安装目录；相对路径基于用户工作目录，解析为绝对路径 |
| `data_dir` | str | `extension/gateway` | Gateway 包存储与数据目录的根；相对路径基于用户工作目录，解析为绝对路径；启动时不创建 |
| `http.allowed_hosts` | list[str] | `[]` | 空表示拒绝一切出站请求；条目为 `host` 或 `host:port` |
| `http.timeout_ms` | int | `3000` | 上限 `5000`；非法值回落默认 |
| `http.max_body_bytes` | int | `1048576` | 上限 `4194304`；非法值回落默认 |
| `http.follow_redirects` | bool | `false` | 非布尔值回落 `false` |
| `uploads.max_bytes` | int | `536870912` | 一次文件导入的总大小；上限 `4294967296`；非法值回落默认，见 [S_10](S_10_application-settings.md) |

配置读取异常时不抛错，等价于空配置。
客户端浏览器能力随包安装激活直接可用，无需额外授权配置；详见 [浏览器接入规约](S_06_local-browser-bridge.md)。

## 3. 包契约

| 项 | 要求 |
| --- | --- |
| 目录 | `<install_dir>/<package_id>/<version>/manifest.json`，或 `<install_dir>/<package_id>/manifest.json` |
| `schemaVersion` | 必须为 `1` |
| `package_type` | 必须为 `extension`；值为 `plugin` 的包归 Agent 插件 |
| `package.id` | 非空标识符 |
| `package.version` | 非空字符串 |
| `package.domain` | 必填；`gateway` 才由 Gateway 加载，只允许 `agentserver` / `gateway` |
| `modules` | 入口模块列表，执行后调用 `create_extension(registrar)` |
| `ui` / `client` | 可选映射，缺省为空；声明贡献和请求绑定，详见 [浏览器接入规约](S_06_local-browser-bridge.md) |
| Handler 层级 | 只能是 USER；登记按包事务化，任一声明失败则整包回滚并报 `FAILED`，不会半注册 |

版本选择：同一 `package.id` 取最高版本目录。

### 3.1 包存储与数据目录

| 能力 | 接口 | 位置 |
| --- | --- | --- |
| 持久化键值 | agent-core `ops.store.get/set/delete/keys`，经 `store.*` 事件链执行，按调用包隔离 | `<data_dir>/store/<package_id>.json` |
| 包数据目录 | `ops.data.root() -> Path`，仅活动包可用，首次调用时创建 | `<data_dir>/packages/<package_id>/` |
| 数据素材 | `ops.ui.asset_url(path, source="data")`，见 [浏览器接入规约](S_06_local-browser-bridge.md) | 包数据目录的 `public/` |
| 导入与导出 | `FileImport` 导入写入 `.uploads/<凭据>/`，Handler 返回后删除；`ops.ui.download(path, *, filename=None)` 导出数据目录内的文件，见 [S_10](S_10_application-settings.md) | 包数据目录 |
| 宿主设置 | 开场的安装顺序、用户顺序与停用记录，卸载时删除该包的记录，见 [S_10](S_10_application-settings.md) | `<data_dir>/host/splash.json` |

`runtime.start()` 在 `ops.build` 前把 `JsonStore(<data_dir>/store)` 绑定到总线，停机时解除绑定。
生命周期：重载、升级（安装更高版本）与停机保留；经管理门面卸载时先删除该包的存储与数据目录，
再删除安装副本；删除数据失败时返回 `EXTENSION_DATA_WRITE_FAILED` 并保留安装副本，再次卸载可重试。
包数据目录及其上级不得为符号链接。

存储调用与 `ops.data.root()` 只接受包**当前激活**交给 Handler 的 Ops：Handler 运行中可用，Handler 返回后
由它派生的后台任务也可继续使用；激活被释放（重载替换、卸载、停机）后，这些 Ops 的数据调用返回 `EVENTS_OPS_INVALID`。
卸载在当前激活仍有 Handler 运行、或该包仍有存储与数据目录调用在途时返回 `EXTENSION_PACKAGE_BUSY`，
不注销 Handler、不删除数据，可稍后重试；不等待在途调用结束。检查先于关闭开场，关闭开场期间新出现的在途调用
在注销前还会再检查一次。包通过 `ops.data.root()` 得到的路径
直接读写文件不经过上述计数，包须在自己的后台任务里自行收尾。设计与验证见 [F_09](../features/F_09_package-store-and-data.md)。

### 3.2 包释放与浏览器通知

包被释放（卸载、重载、发布失败前的替换）时，运行时除注销 Handler 外，还向浏览器发送该包各应用级实例的清除帧
（原因 `released`，并预留该版本号，之后的渲染从更高版本开始），并撤销该包作为提供者的外观（[S_08](S_08_appearance.md)）。
这些通知在释放后异步发送；本机 CLI 的 install 与 reload 成功后，先等待通知发出，再广播 `extensions.v2.installed`，
已挂载的槽位据此重新发送 `ui.ready`。设计与验证见 [F_11](../features/F_11_controlled-appearance.md)。
管理门面卸载、重载或覆盖安装一个活动包之前，先以 `removed` 关闭它正在播放的开场并通知 `ui.closed`
（[S_10 第 5 节](S_10_application-settings.md)）；包释放同时作废它的导入与下载凭据并删除临时导入目录。

## 4. 事件契约

| 事件 | 输入 | 输出 | CORE 默认 |
| --- | --- | --- | --- |
| `gateway.request` | `GatewayRequestInput` | `GatewayRequestOutput` | 校验 method，`forward` |
| `ui.render` | `UiRenderInput` | `UiRenderOutput` | 校验并下发 |
| `ui.ready` / `ui.click` / `ui.select` / `ui.change` / `ui.press_enter` / `ui.search` / `ui.clear` | `UiActionInput` | `UiActionOutput` | 默认未接管；浏览器分派器在触发前校验当前实例及动作 |
| `gateway.disconnect` | `GatewayDisconnectInput` | 无 | 通知插件连接结束，再清理连接资源 |
| `ui.appearance` | `UiAppearanceInput` | `HostActionOutput` | 核对调用包当前激活与浏览器连接，投递或撤销外观（[S_08](S_08_appearance.md)） |
| `ui.download` | `UiDownloadInput` | `HostActionOutput` | 核对激活与连接，签发一次性下载（[S_10](S_10_application-settings.md) 第 7 节） |
| `ui.splash.preview` | `UiSplashPreviewInput` | `HostActionOutput` | 核对激活、开场声明与连接，请求预览（[S_09](S_09_startup-splash.md) 第 3.3 节） |

后三项是宿主动作：包经 `ops.appearance.apply`、`ops.ui.download`、`ops.splash.preview` 触发新链，输入中的激活代次由 Ops 在调用方作用域内填写；CORE 拒绝时返回 `status="rejected"` 与 `INVALID_ACTION`，USER Handler 可以拒绝或接管。

所有事件名满足 `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`；重复注册同名事件返回 `EVENTS_REGISTRATION_INVALID`。

## 5. `gateway.request` 路由契约

| `route` | 必填字段 | 运行时行为 |
| --- | --- | --- |
| `forward` | 无 | 使用 `params` 继续原生分发 |
| `local` | 无 | 交给 Gateway 内置方法表应答 |
| `consume` | `response` | 以成功结果应答，跳过原生分发 |
| `deny` | `error` | 以失败结果应答，跳过原生分发 |
| 其他 | — | 等同 `forward` |

`params` 冻结为只读映射；`session_id` 从 params 的 `session_id` 提取；`headers` 来自浏览器帧的 `request_context.headers`，键统一小写。

完整 `request_context` 独立保留，不合并进 `params`。`webui.dispatch` 同样先经过 USER 请求策略；
继续处理的应用级 UI 包 application 交互由 BUILTIN 浏览器分派器消费。插件拒绝或自行应答的请求不会触发 UI 动作。
HTTP 接入将合法的默认 `forward` 结果继续交给原接口，不要求 `handled=true`。具体范围见 [浏览器接入规约](S_06_local-browser-bridge.md)。

## 6. 扩展鉴权职责

鉴权由 `gateway.request` 的 USER handler 完整负责，包括凭据校验、匿名请求范围、拒绝结果和登录视图。
宿主提供规范化 `headers` 和完整 `request_context`，不把 Authorization 头存在视作认证成功。
没有鉴权扩展时按原生访问流程继续；普通包的安装与卸载不切换登录要求。

USER handler 返回 `deny` 或 `consume` 时，内层 UI 分派器及 CORE 均不执行；
调用 `next_handler` 时，客户端能力包的 application 交互仍由独立的 BUILTIN 浏览器分派器校验和消费。
平台不提供内置认证门禁、专属认证配置、包鉴权标记或登录状态查询方法。
删除与迁移记录见 [F_02](../features/F_02_gateway-request-and-auth-gate.md)。

## 7. 本地方法契约

`GatewayLocalRouter` 初始方法表为空，宿主可通过 `register(method, handler)` 登记本地方法。
`local` 路由只执行已登记的方法；扩展包可直接用 `consume` 返回自身业务应答。

未知方法（包括已移除的 `auth.status`）：`ok=false`，
`error={"code": "ROUTE_UNSUPPORTED", "message": "no gateway builtin for method: <method>"}`。

## 8. 错误码

| 码 | 产生位置 | 含义 |
| --- | --- | --- |
| `BAD_REQUEST` | `gateway.request` CORE | `method` 缺失或非字符串 |
| `ROUTE_UNSUPPORTED` | 本地方法表 | 声明 `local` 但无对应内置方法 |
| `EVENTS_REGISTRATION_INVALID` | agent-core Events | 事件类型非法或重复注册 |
| `EVENTS_OPS_INVALID` | agent-core Events；Gateway 存储与数据调用 | Ops 命名空间或方法重复注册；已释放激活的 Ops 调用存储或 `ops.data.root()` |
| `EXTENSION_PACKAGE_BUSY` | 管理门面卸载 | 包仍有 Handler 或数据调用在途，本次卸载未做任何修改 |

渲染与 HTTP 的错误码分别在 S_02 与 S_05 维护。

## 9. 不变量

1. 每个进程只有一个 `GatewayAssembly`；`shutdown_gateway_extensions` 之后可重新装配。
2. `ops.build` 只在 `runtime.start()` 内执行一次，先于任何包发布。
3. `runtime.active` 只统计 USER handler。
4. 帧拦截超时 5 秒；超时或异常按 `forward` 继续。
5. 停机会注销全部 USER 与 BUILTIN handler 并清空进程内状态存储（`UiStateStore`）；包存储与数据目录保留。
6. `extensions_v2` 不 import `jiuwenswarm.extensions_v2`，也不 import AgentServer 业务模块。
7. connection 作用域状态在 socket 关闭时清除；session 与 global 由写入方或停机清理。
8. 包只能通过 Handler 的包身份访问自己的存储与数据目录，不能指定其他包。
9. 包存储与数据目录只在卸载时删除；删除失败不删除安装副本。
10. 包释放后，浏览器不再显示该包的应用级实例与外观；释放时发送的清除帧版本号不会被之后的渲染复用。
11. 每次加载包（启动、安装、重装、重载）都使用新的模块名：在同一路径重装同一版本也执行新的代码，不复用上一次加载缓存在
    `sys.modules` 中的同包子模块。本次加载创建的模块在激活释放（卸载、重载替换、停机）且该激活的在途 Handler 调用全部结束后
    从 `sys.modules` 移除，包括入口导入的同包子模块；加载或发布失败时立即移除。重复发布同一次加载时模块随之转交新的激活。
    移除只解除 `sys.modules` 的引用，仍被其他对象引用的模块不会被强制回收。
12. 卸载成功之后，该包已释放激活的 Ops 不能再写入存储或创建数据目录；有调用在途时卸载返回 `EXTENSION_PACKAGE_BUSY`
    且不注销 Handler、不删除数据。已释放的激活不再开始新的 Handler 调用：释放前已收集该 Handler 的事件链会跳过它。

## 10. 验证索引

| 契约 | 用例 |
| --- | --- |
| 入口幂等与降级 | `test_gateway_request.py` |
| 渠道断线钩子（WebChannel 与 GatewayServer 的调用方式）释放连接状态 | `test_gateway_request.py` |
| 同版本重装加载新代码与释放后清理模块（不变量 11） | `test_gateway_package_management.py` |
| 四路由 | `test_gateway_request.py` |
| 扩展鉴权与匿名请求内传 | `test_gateway_request.py`、`test_gateway_package_management.py` |
| application 槽位交互与请求策略 | `test_gateway_browser_bridge.py` |
| 本地方法表 | `test_gateway_request.py` |
| 渲染与槽位契约 | `test_gateway_ui_render.py`（契约细节见 [S_02](S_02_ui-render.md)） |
| 出站 HTTP 白名单与预算 | `test_gateway_http_fetch.py`（契约细节见 [S_05](S_05_http-fetch.md)） |
| 包存储、数据目录、数据素材与卸载清理；在途调用与已释放激活的数据调用（不变量 12） | `test_gateway_package_data.py` |
| 应用级 UI 包与素材路由 | `test_gateway_application_assets.py` |
| 包释放通知、受控外观与重载广播 | `test_gateway_appearance.py`、`test_gateway_package_management.py`（契约细节见 [S_08](S_08_appearance.md)） |
| 宿主方法、开场偏好、导入与导出 | `test_gateway_application_settings.py`、`test_gateway_file_transfer.py`（契约细节见 [S_10](S_10_application-settings.md)） |


## 本机扩展管理 CLI

Gateway 装配完成后创建本机管理入口，关闭运行时时撤销入口。

- **包管理：** `extensions_v2/management/local_cli.py` 调用当前 Gateway 的包管理门面，不导入 AgentServer 扩展模块。
- **宿主归属：** install 校验 ZIP 的 `package.domain` 为 gateway；query/reload/uninstall 仅处理本宿主包。
- **界面通知：** install 与 reload 成功后广播 `extensions.v2.installed`（见第 3.2 节）。
- **访问权限：** 访问密钥与实例标识由宿主生成，只供当前操作系统账号使用。Web 登录和认证继续使用原有规则。

公共命令和退出码见[本机 CLI 契约](../../../../extensions_v2/docs/specs/S_02_backend-package-management.md#5-本机-cli)，
实施与验证见 [F_31](../../../../extensions_v2/docs/features/F_31_extension-cli.md)。
