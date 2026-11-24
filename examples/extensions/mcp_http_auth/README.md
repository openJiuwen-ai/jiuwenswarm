# MCP Per-Request HTTP Auth

在**每一次**发往 MCP 的 HTTP 请求之前注入鉴权头。用的是核心的
`agent_server:before_mcp_http` 钩子（`jiuwenswarm/common/mcp_http_hook.py`），
不是「每轮对话一次」。

## 什么时候用它

| 场景 | 用哪个 |
| :--- | :--- |
| 每轮对话一个令牌，一个连接跑完整轮 | 不用扩展：`chat.send` 带 `mcp_runtime_params`（注册时声明 `runtime_params`），值进池键、按用户分连接 |
| 网关要求每请求换令牌 / 每调用签名 / 一次性票据 | **本扩展**（`before_mcp_http`） |
| 每用户令牌 + 每请求轮换（两者都要） | 上面两条一起用：运行时参数负责隔离连接，本扩展负责逐请求换头 |
| 全公司一个固定令牌 | 不用扩展，写 `config.yaml` 或注册条目 `auth_headers` |

**关键区别**：`mcp_runtime_params` 的值会进连接池键，不同值各用各的连接；
`before_mcp_http` 只改本次请求的头，**不换连接**。需要「A 用户的令牌不能用在 B 的连接上」时，
只靠本扩展是不够的。

## 触发点

`before_mcp_http` 在 openjiuwen 的 header/query 鉴权 provider 里触发，因此覆盖：

- `initialize`（建连）
- `tools/list`（列工具，含周期扫描与复扫）
- `tools/call`（每次调用）
- SSE 的建流 GET 与后续 POST 消息

每次都会调用扩展，扩展拿到的是：

```python
McpHttpHookContext(
    server_name="corp-mcp-gw",
    method="tools/call",      # 从 JSON-RPC body 推；GET 建流为 "stream"
    request_id="req-8f3c",    # 调用所属请求
    session_id="sess-21",
    headers={},               # 扩展往这里写
)
```

`method` 从请求体里的 JSON-RPC 方法推出来；**无人订阅本事件时宿主直接短路**，不解析 body。

`request_id` / `session_id` 由调用方经 ContextVar 传递（工具对象在 `invoke` 期间绑定、
结束后复位，worker 在自己的 owner task 里再绑一次）。这条链路有边界：走本仓 worker 的
路径（streamable-http、请求级池）能拿到；SDK 自持 owner task 的路径（如 SSE 的
owner loop）发出的请求可能拿不到——此时两个字段为空串，**头照常生效**，只是扩展拿不到
请求归属。需要严格归属时优先用 streamable-http。

## 启用

父目录（含有 `mcp_http_auth/` 的那一层）加进扩展搜索路径：

```yaml
extensions:
  extension_dirs: "D:/path/to/extensions"
  mcp_http_auth:
    enabled: true
    header_name: Authorization
    token_prefix: "Bearer "
    token_env: CORP_MCP_TOKEN
    servers:
      corp-mcp-gw: {}
```

不写 `servers` 时对所有 MCP 生效；写了就只对列出的服务注入。

**配置解析的三个边界（都按 fail-closed 处理）：**

- `enabled` 按真值解析而不是 `bool()`：`"true"/"1"/"yes"/"on"` 为真，
  `"false"/"0"/"no"/"off"/空` 为假，其它取值打 warning 并按**禁用**处理——否则
  `enabled: "false"` 会被 `bool()` 判成真，把企业令牌注入到所有 MCP。
- `servers` 必须是映射（`{名字: {...}}`）。写成列表或字符串会打 warning 并**整体禁用**，
  而不是静默退化成「对所有服务注入」。
- `token_prefix` 缺省或显式为 null 时回落 `Bearer `；只有显式写空串 `""` 才表示不加前缀，
  避免 `str(None)` 生成 `Authorization: Nonetoken`。

## 换 token 的真实实现

示例默认从环境变量取。生产环境把 `register_extensions` 里的
`make_handler(settings)` 换成 `make_handler(settings, token_provider=lookup)`，
`lookup(server_name, method)` 每个请求都会被调一次，可以在这里调 STS、查密钥库、
或者对 method + URL 做签名，**不要缓存**，否则短时令牌会过期。

## 宿主保留的头

扩展写下面这些头会被丢弃（`mcp_http_hook.PROTECTED_HEADERS`）：

`accept`、`accept-encoding`、`connection`、`content-encoding`、`content-length`、
`content-type`、`host`、`mcp-protocol-version`、`mcp-session-id`、`te`、`trailer`、
`transfer-encoding`、`upgrade`。

这些是报文级/协商类头：把 `header_name` 配成 `Content-Encoding` 会让服务端按错误的编码解 body，
配成 `MCP-Protocol-Version` 会打断协议协商，所以即使写了也一律丢弃。

值还会过滤：头名必须合法、不能含 `\r` `\n` `\0`、必须是字符串、单值不超过 32768 字符，
且必须能被 latin-1 编码（HTTP/1.1 头值限制；写中文会在这里被丢弃，而不是让 httpx 编码时报错）。
合并写入还包了一层 `try/except`：万一值被传输层拒绝，只丢这一个头并记 warning，请求照常发出。
扩展抛错时本次请求回落到静态配置头，MCP 调用不会因此失败。

## 和连接复用的关系

钩子只改这次请求的头，**不重建连接**。所以：

- 每请求换令牌且网关不校验会话绑定：可以继续复用连接；
- 网关在 `initialize` 时把身份绑到会话：必须让连接随请求独占
  （注册时 `connection_scope: request`），否则后续请求换了头也可能被会话身份覆盖。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/extensions/test_mcp_http_auth_example.py -o addopts="--asyncio-mode=auto" -q
.venv\Scripts\python.exe -m pytest tests/unit_tests/common/test_mcp_http_hook.py -o addopts="--asyncio-mode=auto" -q
```

前者测示例扩展，后者测核心钩子（协议头保护、值校验、失败回落、ContextVar 传递、幂等）。
