# Gateway 扩展运行时规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_01 / gateway-extension-runtime` |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/` |
| 设计依据 | [详细架构](../designs/extensions-v2-architecture.md) |
| 最近一次修订日期 | 2026-11-04 |
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
| `http.allowed_hosts` | list[str] | `[]` | 空表示拒绝一切出站请求；条目为 `host` 或 `host:port` |
| `http.timeout_ms` | int | `3000` | 上限 `5000`；非法值回落默认 |
| `http.max_body_bytes` | int | `1048576` | 上限 `4194304`；非法值回落默认 |
| `http.follow_redirects` | bool | `false` | 非布尔值回落 `false` |

配置读取异常时不抛错，等价于空配置。
客户端浏览器能力随包安装激活直接可用，无需额外授权配置；详见 [浏览器接入规约](S_06_local-browser-bridge.md)。

## 3. 包契约

| 项 | 要求 |
| --- | --- |
| 目录 | `<install_dir>/<package_id>/<version>/manifest.yaml`，或 `<install_dir>/<package_id>/manifest.yaml` |
| `schemaVersion` | 必须为 `1` |
| `package.id` | 非空标识符 |
| `package.version` | 非空字符串 |
| `domain` | `gateway` 才由 Gateway 加载；缺省视为 `agent`，只允许 `agent` / `gateway` |
| `modules` | 入口模块列表，执行后调用 `create_extension(registrar)` |
| `ui` / `client` | 可选映射，缺省为空；声明贡献和请求绑定，详见 [浏览器接入规约](S_06_local-browser-bridge.md) |
| Handler 层级 | 只能是 USER；登记按包事务化，任一声明失败则整包回滚并报 `FAILED`，不会半注册 |

版本选择：同一 `package.id` 取最高版本目录。

## 4. 事件契约

| 事件 | 输入 | 输出 | CORE 默认 |
| --- | --- | --- | --- |
| `gateway.request` | `GatewayRequestInput` | `GatewayRequestOutput` | 校验 method，`forward` |
| `ui.render` | `UiRenderInput` | `UiRenderOutput` | 校验并下发 |
| `ui.ready` / `ui.click` / `ui.select` / `ui.change` / `ui.press_enter` / `ui.search` / `ui.clear` | `UiActionInput` | `UiActionOutput` | 默认未接管；浏览器分派器在触发前校验当前实例及动作 |
| `gateway.disconnect` | `GatewayDisconnectInput` | 无 | 通知插件连接结束，再清理连接资源 |

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
继续处理的客户端能力包 application 交互由 BUILTIN 浏览器分派器消费。插件拒绝或自行应答的请求不会触发 UI 动作。
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
| `EVENTS_OPS_INVALID` | agent-core Events | Ops 命名空间或方法重复注册 |

渲染与 HTTP 的错误码分别在 S_02 与 S_05 维护。

## 9. 不变量

1. 每个进程只有一个 `GatewayAssembly`；`shutdown_gateway_extensions` 之后可重新装配。
2. `ops.build` 只在 `runtime.start()` 内执行一次，先于任何包发布。
3. `runtime.active` 只统计 USER handler。
4. 帧拦截超时 5 秒；超时或异常按 `forward` 继续。
5. 停机会注销全部 USER 与 BUILTIN handler 并清空状态存储。
6. `extensions_v2` 不 import `jiuwenswarm.extensions_v2`，也不 import AgentServer 业务模块。
7. connection 作用域状态在 socket 关闭时清除；session 与 global 由写入方或停机清理。

## 10. 验证索引

| 契约 | 用例 |
| --- | --- |
| 入口幂等与降级 | `test_gateway_request.py` |
| 四路由 | `test_gateway_request.py` |
| 扩展鉴权与匿名请求内传 | `test_gateway_request.py`、`test_gateway_package_management.py` |
| application 槽位交互与请求策略 | `test_gateway_browser_bridge.py` |
| 本地方法表 | `test_gateway_request.py` |
| 渲染与槽位契约 | `test_gateway_ui_render.py`（契约细节见 [S_02](S_02_ui-render.md)） |
| 出站 HTTP 白名单与预算 | `test_gateway_http_fetch.py`（契约细节见 [S_05](S_05_http-fetch.md)） |
