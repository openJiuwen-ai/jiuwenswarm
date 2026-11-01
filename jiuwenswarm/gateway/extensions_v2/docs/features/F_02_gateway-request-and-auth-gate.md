# gateway.request 与认证门禁设计方案

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | feature |
| 编号 / slug | `F_02 / gateway-request-and-auth-gate` |
| 关联计划 | EXTV2-GW-02 `gateway.request` 与认证门禁 |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/{models,event_types/gateway,core_handlers/request_core,builtin/auth_gate,builtin/router}.py` |
| 设计依据 | [详细架构第 5 节](../designs/extensions-v2-architecture.md#5-事件体系)、[运行时规约](../specs/S_01_gateway-extension-runtime.md) |
| 最近一次修订日期 | 2026-11-01 |
| 方案状态 | 已实现，单元验证通过 |

## 1. 问题与目标

Gateway 的 WebSocket 帧原本直接进入原生分发：方法名决定路由，未识别的帧返回错误。若要在分发给 AgentServer 之前做准入判断（例如未登录连接不得发送任何业务请求），需要一个统一的、可被扩展包参与的拦截点。

一句话概括：**每个上行帧在原生分发前经过 `gateway.request` 事件链，链的处置结果决定帧是继续转发、本地应答、直接消费还是被拒绝；认证门禁是该事件的 BUILTIN 层平台规则。**

## 2. 范围

### 2.1 本次范围

- 定义 `gateway.request` 事件类型与 `match_pattern_subset` 匹配。
- `GatewayRequestInput` / `GatewayRequestOutput` 与四路由语义。
- CORE Handler：校验 method，默认 `forward`。
- BUILTIN 认证门禁：未登录连接只放行白名单方法。
- `local` 路由的内置方法表与 `auth.status`。
- 运行时把处置结果落实到渠道应答或原生分发。

### 2.2 非目标

- 不实现登录本身，登录由 USER 包 Handler 完成。
- 不实现界面渲染；门禁只预留 `on_locked` 回调，渲染在 F_03 接入。
- 不覆盖 IM 渠道与 TUI 流量，只覆盖 Web 渠道 WebSocket 帧。
- 不改变 Gateway 原有方法路由表。

## 3. 总体设计

### 3.1 组件关系

```mermaid
flowchart LR
    WS["WebChannel 帧"] --> RT["GatewayRuntime.intercept"]
    RT --> BUS["ExtensionEventBus"]
    BUS -->|"USER"| P["包 Handler"]
    P -->|"next"| G["BUILTIN 认证门禁"]
    G -->|"next"| C["CORE request_core"]
    C --> OUT["GatewayRequestOutput"]
    OUT --> RT
    RT -->|"forward"| NATIVE["原生分发"]
    RT -->|"local"| ROUTER["GatewayLocalRouter"]
    RT -->|"consume / deny"| RESP["直接应答"]
    G -->|"deny"| RESP
```

### 3.2 输入输出

```python
@dataclass(frozen=True, slots=True)
class GatewayRequestInput:
    method: str
    params: Mapping[str, Any] = ...
    session_id: str | None = None
    connection_id: str = ""
    user_id: str | None = None
    channel_id: str = "web"

@dataclass(frozen=True, slots=True)
class GatewayRequestOutput:
    handled: bool = False
    route: str = "forward"
    params: Mapping[str, Any] | None = None
    response: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    meta: Mapping[str, Any] | None = None
```

`params` 使用 `MappingProxyType` 冻结，避免包 Handler 无意修改原帧。

### 3.3 路由落实

| 路由 | 运行时动作 | 原生分发 |
| --- | --- | --- |
| `forward` | 使用返回的 `params`（缺省回退原 params） | 执行 |
| `local` | `GatewayLocalRouter.execute` 后应答 | 跳过 |
| `consume` | 以 `response` 成功应答 | 跳过 |
| `deny` | 以 `error` 失败应答 | 跳过 |
| 未知/缺省 | 等同 `forward` | 执行 |

应答统一经 `channel.send_response(ws, req_id, ok=..., payload=..., error=..., code=...)`，兼容渠道已有的响应格式。

### 3.4 认证门禁

放行顺序（`_lets_through`）：

1. `connection_id` 为空：宿主内部触发，没有浏览器连接可认证；
2. `method` 为空或非字符串：交给 CORE 返回 `BAD_REQUEST`；
3. `method in LOCKED_ALLOWED_METHODS`：`webui.dispatch` 与 `ping`；
4. `is_authenticated(store, connection_id)`：连接作用域存在 `auth.session`。

拒绝时：

- 若注入了 `on_locked`（连接 id 非空），调用一次；异常只告警，不改变拒绝结果；
- 返回 `GatewayRequestOutput(handled=True, route="deny", error={"code": "AUTH_REQUIRED", "message": "login required"})`。

门禁只读 `auth.session`，写入方是 USER 包 Handler。

### 3.5 层级后果

`ExtensionEventBus` 的 USER 层在最外层。包 Handler 若不调用 `next_handler`，门禁与 CORE 都不会执行。这是有意保留的契约：包可以替换平台行为（例如自定义准入），平台规则只约束继续内传的帧。该契约必须在包文档中说明。

### 3.6 本地路由

| 方法 | 行为 |
| --- | --- |
| `auth.status` | `{"authenticated": bool}`，读取调用的 connection_id |
| 其他 | `ok=false`，`code=ROUTE_UNSUPPORTED`，`message="no gateway builtin for method: <method>"` |

未知方法显式失败而不是回退转发，避免包误以为本地已处理而漏掉真实请求。

## 4. 处理流程

### 4.1 正常帧

```mermaid
sequenceDiagram
    participant C as WebChannel
    participant R as GatewayRuntime
    participant B as EventBus
    participant P as 包 Handler
    participant K as CORE
    C->>R: intercept(ws, req_id, method, params)
    R->>B: trigger(gateway.request)
    B->>P: USER（匹配则执行）
    P->>K: next_handler
    K-->>R: {route: forward}
    R-->>C: (False, params)
    C->>C: 原生分发
```

### 4.2 未登录帧

```mermaid
sequenceDiagram
    participant C as WebChannel
    participant R as GatewayRuntime
    participant B as EventBus
    participant G as 认证门禁
    participant W as 浏览器
    C->>R: intercept(ws, req_id, "chat.send", params)
    R->>B: trigger(gateway.request)
    B->>G: BUILTIN（无 USER handler 匹配）
    G-->>R: {route: deny, error: AUTH_REQUIRED}
    R->>W: send_response(ok=false, code=AUTH_REQUIRED)
    R-->>C: (True, params)
    Note over C: 跳过原生分发
```

### 4.3 本地应答

声明 `local` 的 USER handler（例如查询登录态的包装）让运行时调用 `GatewayLocalRouter`。`auth.status` 的返回直接来自连接上的 `auth.session`，不经过 AgentServer。

## 5. 异常与边界

| 情况 | 行为 |
| --- | --- |
| 事件链超时（5s） | 告警，按 `forward` 继续 |
| Handler 抛异常 | 告警，按 `forward` 继续 |
| `on_locked` 渲染失败 | 告警，仍按 `deny` 拒绝 |
| `params` 非 Mapping | 入口直接返回未访问 |
| 未注册事件类型 | 不可能：装配阶段先注册，异常会被降级处理 |
| `send_response` 不可用 | 静默跳过，帧仍视为已处置 |

## 6. 实施影响

- 新增 `models.py` 的路由枚举与两个数据类，`event_types/gateway.py`、`core_handlers/request_core.py`、`builtin/auth_gate.py`、`builtin/router.py`。
- `WebChannel` 原生分发前增加一次拦截调用；`visited=True` 时不再分发。
- 认证事实存放在 `UiStateStore` 的 connection 作用域，断线自动清除。
- 与 AgentServer 的 `ui.click` 等事件无耦合；Gateway 侧事件只在 Gateway 注册。

## 7. 验证

验证入口：`tests/unit_tests/gateway/extensions_v2/test_gateway_request.py`、`test_gateway_auth_gate.py`。

| 场景 | 结果 |
| --- | --- |
| 无 USER handler 时 CORE 默认 `forward` | 通过 |
| USER handler 返回 `consume` 时直接应答且不分发 | 通过 |
| USER handler 返回 `deny` 时返回错误码 | 通过 |
| `local` 路由由内置 router 应答 `auth.status` | 通过 |
| 未知本地方法返回 `ROUTE_UNSUPPORTED` | 通过 |
| 未认证连接发送业务方法被 `AUTH_REQUIRED` 拒绝 | 通过 |
| 已认证连接到达 CORE | 通过 |
| `webui.dispatch` 在白名单内放行 | 通过 |
| 空 method 交给 CORE 返回 `BAD_REQUEST` | 通过 |
| 无 connection 的宿主帧不拦截 | 通过 |
| `on_locked` 每次拒绝调用一次 | 通过 |
| `ops.gateway.request` 可嵌套触发 | 通过 |

## 8. 风险与遗留

- 门禁不覆盖 IM 渠道与 TUI；这些入口需要各自的准入方案。
- `webui.dispatch` 在锁定期完全放行，登录框上的按钮点击才可能到达包 Handler；若包未处理该点击，帧会落到原生分发并返回未知方法错误。
- 认证只有"存在即已认证"的语义，没有过期时间；需要过期时由包写入 TTL 并依赖 `UiStateStore` 的 TTL 清理。
- 拒绝应答的字段名依赖渠道 `send_response` 的实现；更换渠道需核对 `code`/`error` 字段。
