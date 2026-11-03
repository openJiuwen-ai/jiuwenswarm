# Gateway 扩展运行时规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_01 / gateway-extension-runtime` |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/` |
| 设计依据 | [详细架构](../designs/extensions-v2-architecture.md) |
| 最近一次修订日期 | 2026-11-03 |
| 契约状态 | 已实现；本文是 Gateway 扩展对外契约的唯一维护位置 |

本文维护 Gateway 扩展运行时的对外契约：入口函数、事件数据类、路由语义、错误码、配置键与不变量。具体实现方案与决策历史见对应 feature 文档。

## 1. 入口契约

| 入口 | 签名 | 语义 |
| --- | --- | --- |
| `init_gateway_extensions` | `async (channel, config=None) -> GatewayRuntime \| None` | 装配并启动；已装配时返回同一实例；禁用或失败返回 `None` |
| `intercept_gateway_frame` | `async (ws, req_id, method, params, headers=None) -> tuple[bool, dict]` | 返回 `(visited, params)`；`visited=True` 表示已处置，原生分发必须跳过 |
| `notify_gateway_disconnect` | `async (ws) -> None` | 清理该连接的 connection 作用域状态；不抛异常 |

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
| `auth.enabled` | bool | `true` | `false` 时门禁不生效，等价于无准入 |

配置读取异常时不抛错，等价于空配置。

## 3. 包契约

| 项 | 要求 |
| --- | --- |
| 目录 | `<install_dir>/<package_id>/<version>/manifest.yaml`，或 `<install_dir>/<package_id>/manifest.yaml` |
| `schemaVersion` | 必须为 `1` |
| `package.id` | 非空标识符 |
| `package.version` | 非空字符串 |
| `domain` | `gateway` 才由 Gateway 加载；缺省视为 `agent`，只允许 `agent` / `gateway` |
| `auth_provider` | 可选布尔，缺省 `false`。声明 `true` 表示该包拥有登录流程，门禁仅在有此类包时生效；**该包必须至少注册一个 handler**——平台只按此声明决定门禁，不校验 handler 数量，声明了却一个都没注册会导致门禁打开但无登录动作 |
| `modules` | 入口模块列表，执行后调用 `create_extension(registrar)` |
| Handler 层级 | 只能是 USER；登记按包事务化，任一声明失败则整包回滚并报 `FAILED`，不会半注册 |

版本选择：同一 `package.id` 取最高版本目录。

## 4. 事件契约

| 事件 | 输入 | 输出 | CORE 默认 |
| --- | --- | --- | --- |
| `gateway.request` | `GatewayRequestInput` | `GatewayRequestOutput` | 校验 method，`forward` |
| `ui.render` | `UiRenderInput` | `UiRenderOutput` | 校验并下发 |
| `ui.click` | `UiClickInput` | `UiClickOutput` | 校验实例并回执 |

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

## 6. 认证门禁契约

门禁行为由 `extensions.gateway.auth` 的策略驱动（见第 2 节）。认证事实由浏览器 `localStorage` 持有：登录成功后扩展包让浏览器写入 token，之后每帧在 `request_context.headers.authorization` 携带；Gateway 不保存、不解析、不校验 token，只判断该头是否存在且非空，token 有效性由 USER handler 决定。

不生效条件（任一成立）：`auth.enabled=false`；无 `auth_provider` 包；无 `connection_id`（宿主内部触发，没有浏览器连接可认证）；`method` 为空或非字符串（交给 CORE 返回 `BAD_REQUEST`）。

生效时按以下顺序处理：

| 顺序 | 条件 | 结果 |
| --- | --- | --- |
| 1 | `method="webui.dispatch"` 且目标槽在 `SLOT_SPECS` 中登记为 `application` 作用域，且 `params.event == "ui.ready"` | `route="consume"`，`response={"event": "ui.ready", "accepted": true}` |
| 2 | 同上但 `params.event == "ui.change"` | `route="consume"`，`response={}` |
| 3 | `headers.authorization` 存在且非空 | 继续内传 |
| 4 | `method="ping"` | 继续内传 |
| 5 | 其他命中 Gateway 自有 application 槽位的 `webui.dispatch` | `route="consume"`，`response={}` |
| 6 | 其他 | `route="deny"` |

application 槽位帧由门禁本地应答，因为这些槽位没有 AgentServer 视图：

- `ui.ready` 表示前端已挂载该槽位，无需转发；
- `ui.change` 是表单草稿变化，字段值随 `ui.click` 上行，草稿直接丢弃；
- 其他事件（含未被包接管的 `ui.click`）回空载荷，避免落到原生分发的未知方法错误。

拒绝结果：`route="deny"`，`error={"code": "AUTH_REQUIRED", "message": "login required"}`。

不变量：

- 门禁不保存任何服务端会话，不渲染任何视图；由扩展包自行渲染登录界面；
- 门禁只判断 `authorization` 头是否存在且非空，不解析、不缓存、不撤销凭据；
- 门禁位于 BUILTIN 层，USER handler 不调用 `next_handler` 时不会执行；
- 门禁不计入 `runtime.active`。

## 7. 本地方法契约

| 方法 | 成功返回 |
| --- | --- |
| `auth.status` | `ok=true`，`payload={"authenticated": bool}` |

未知方法：`ok=false`，`error={"code": "ROUTE_UNSUPPORTED", "message": "no gateway builtin for method: <method>"}`。

## 8. 错误码

| 码 | 产生位置 | 含义 |
| --- | --- | --- |
| `AUTH_REQUIRED` | 认证门禁 | 连接未携带 `authorization` 头且不属于豁免帧 |
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
| 门禁放行与拒绝 | `test_gateway_auth_gate.py`、`test_gateway_ui_render.py` |
| application 槽位 `ui.ready` / `ui.change` | `test_gateway_auth_gate.py` |
| 本地方法表 | `test_gateway_auth_gate.py`、`test_gateway_request.py` |
| 渲染与槽位契约 | `test_gateway_ui_render.py`（契约细节见 [S_02](S_02_ui-render.md)） |
| 出站 HTTP 白名单与预算 | `test_gateway_http_fetch.py`（契约细节见 [S_05](S_05_http-fetch.md)） |
