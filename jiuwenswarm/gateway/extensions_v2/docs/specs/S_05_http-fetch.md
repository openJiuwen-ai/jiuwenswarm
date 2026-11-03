# ops.http.fetch 契约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_05 / http-fetch` |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/handler_ops/http_ops.py` |
| 设计依据 | [F_06 HTTP 能力与示例包](../features/F_06_http-fetch.md) |
| 最近一次修订日期 | 2026-11-01 |
| 契约状态 | 已实现；本文维护 `ops.http.fetch` 的对外契约 |

## 1. 能力契约

| 项 | 内容 |
| --- | --- |
| 方法 | `ops.http.fetch(request)` |
| 命名空间 | `http`，注册位置 `handler_ops/http_ops.py` |
| 事件 | 通过 `ops.build` 注册，不是独立事件类型 |
| 输入 | `HttpFetchInput` |
| 输出 | `HttpFetchOutput`（不抛业务异常） |
| 触发点 | 由包 Handler 主动调用 |

`HttpFetchInput`：

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `url` | str | 必填 | scheme 必须为 `http` / `https`，主机必须在白名单内 |
| `method` | str | `GET` | 大写后发送；`GET` / `HEAD` 不发送请求体 |
| `headers` | `Mapping[str, str]` | `{}` | 原样发送；跨主机重定向时剔除 `authorization` / `cookie` / `proxy-authorization` |
| `body` | str / bytes / `Mapping` / list | `None` | 映射与列表按 JSON 编码；301/302/303 重定向后丢弃 |
| `timeout_ms` | int | 3000 | 生效值为 `min(请求值, 5000)`，是整次调用（含重定向）的总预算；非正整数回退策略默认值 |
| `follow_redirects` | bool | `false` | 与策略值取或；最多跟随 3 跳，每跳重新校验白名单 |

`HttpFetchOutput`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | int | HTTP 状态码；平台层拒绝时为 `0` |
| `headers` | `Mapping[str, str]` | 响应头，键统一小写 |
| `body` | Any | `content-type` 含 `json` 时为解析结果，否则为文本；失败时为 `None` |
| `elapsed_ms` | int | 从发起到返回的毫秒数 |
| `code` | `str \| None` | 平台层失败码 |

## 2. 配置契约

配置键 `extensions.gateway.http`：

| 键 | 类型 | 默认 | 上限 / 约束 |
| --- | --- | --- | --- |
| `allowed_hosts` | `list[str]` | `[]` | `host` 或 `host:port`；空表拒绝一切 |
| `timeout_ms` | int | 3000 | 不超过 5000 |
| `max_body_bytes` | int | 1 MiB | 不超过 4 MiB |
| `follow_redirects` | bool | `false` | 与请求标志取或 |

白名单条目匹配规则：`host` 条目允许该主机的任意端口；`host:port` 条目只允许指定端口，也会匹配省略默认端口的 URL（`https` 对应 443，`http` 对应 80）；IPv6 字面量写成 `[::1]` 或 `[::1]:8080`；scheme 只接受 `http` 与 `https`。客户端不读取 `HTTP(S)_PROXY` 等代理环境变量，白名单校验的主机就是实际连接的主机。

## 3. 错误码

| 码 | 产生条件 | `status` | `body` |
| --- | --- | --- | --- |
| `HTTP_HOST_DENIED` | 主机不在白名单，或重定向目标不在白名单 | `0` / 3xx | 空 |
| `HTTP_TIMEOUT` | 请求超过生效超时 | `0` | 空 |
| `HTTP_BODY_TOO_LARGE` | 响应体超过 `max_body_bytes` | 原状态码 | 空 |
| `HTTP_REQUEST_FAILED` | DNS、连接、TLS 等传输异常 | `0` | 空 |

除上述四码外没有平台层失败；4xx/5xx 属于对端响应，按正常结果返回 `status` 与 `body`。

## 4. 不变量

1. 未命中白名单时不发起任何网络请求。
2. 重定向的每一跳都重新校验白名单，最多 3 跳。
3. 301/302/303 重定向把非 `GET` 请求降级为无 body 的 `GET`；307/308 保留原方法与 body。跨主机时剔除凭据头。
4. 响应体流式读取，累计字节数超过 `max_body_bytes` 立即中断并返回错误码，绝不截断后交给包。
5. `ops.http.fetch` 不修改事件链，不调用 `next_handler`。
6. 从发起到返回共享一个 deadline，单次请求的超时不超过 5000 毫秒，重定向不会重置该预算；响应体上限不超过 4 MiB。
7. 日志只记录主机名与异常类型，不记录请求头、请求体或响应体。

## 5. 与 AgentServer 的差异

| 维度 | AgentServer | Gateway |
| --- | --- | --- |
| 命名空间 | 只有 `session` 与 `ui`，无 `http` | 提供 `http` 命名空间 |
| 出站来源 | 由具体能力（如文件读取）内部完成 | 由扩展包显式调用 |
| 策略来源 | 无统一出站策略 | `extensions.gateway.http` 统一约束 |

`ops.http.fetch` 只在 Gateway 侧存在；`domain: agent` 的包调用会因命名空间缺失而失败，无法借用 Gateway 的出站能力。

## 6. 示例包契约

`examples/auth_login_demo` 声明 `domain: gateway`，入口 `auth_login_demo/extension.py`，注册两条 `ui.click`：

| 动作 | 匹配 | 出站 | 成功副作用 |
| --- | --- | --- | --- |
| `auth.create` | `{"target.slot_id": "application.gate", "action_id": "auth.create"}` | `POST /v1/users` | `ops.ui.store("auth.session", {token, user_id}, ttl_s=expires_in)` + `ops.ui.render` 成功视图 |
| `auth.login` | `{"target.slot_id": "application.gate", "action_id": "auth.login"}` | `POST /v1/login` | 同左 |

服务契约（`mock_auth_service.py` 实现同一契约）：

```text
POST /v1/users   {"username", "password"}
  → 201 {"user_id", "auth_token", "expires_in"} | 409 {"error": "user_exists"}
POST /v1/login   {"username", "password"}
  → 200 {"user_id", "auth_token", "expires_in"} | 401 {"error": "invalid_credentials"}
```

`UiClickOutput.status` 取值：`created`、`logged_in`、`missing_fields`、`user_exists`、`invalid_credentials`、`unavailable`、`store_failed`、`render_failed`。

## 7. 验证索引

| 契约 | 用例 |
| --- | --- |
| 空白名单拒绝 | `test_gateway_http_fetch.py::test_empty_allowlist_denies_every_host` |
| `host:port` 匹配 | `test_gateway_http_fetch.py::test_host_port_entry_matches_only_that_port` |
| 请求体与 JSON 解析 | `test_gateway_http_fetch.py::test_allowed_host_returns_the_parsed_json_body` |
| 响应体上限 | `test_gateway_http_fetch.py::test_response_over_the_body_budget_is_reported_not_truncated` |
| 超时 | `test_gateway_http_fetch.py::test_timeout_is_mapped_to_a_code` |
| 重定向逐跳校验 | `test_gateway_http_fetch.py::test_redirect_to_a_denied_host_is_refused`、`test_redirects_are_not_followed_by_default` |
| 跨主机重定向剔除凭据并降级 GET | `test_gateway_http_fetch.py::test_cross_host_redirect_drops_credentials_and_switches_to_get` |
| 不读取代理环境变量 | `test_gateway_http_fetch.py::test_client_ignores_proxy_environment` |
| IPv6 与默认端口条目 | `test_gateway_http_fetch.py::test_ipv6_and_default_port_entries_match` |
| 传输失败 | `test_gateway_http_fetch.py::test_request_failure_is_reported_as_a_code` |
| 示例包成功路径 | `test_auth_login_demo.py::test_login_success_stores_the_session_and_repaints` |
| 示例包失败路径 | `test_auth_login_demo.py::test_create_reports_an_existing_user_and_keeps_the_form`、`test_wrong_password_is_reported_without_storing`、`test_denied_host_is_reported_as_unavailable` |
| 示例包入参校验 | `test_auth_login_demo.py::test_missing_fields_never_reach_the_auth_service` |
| 示例包动作范围 | `test_auth_login_demo.py::test_other_actions_fall_through_the_chain` |
