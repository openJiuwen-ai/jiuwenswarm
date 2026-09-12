# 3rd Agent Web 访问通道设计文档

> **目标**：在 AgentOS Router 模式下，通过网关透明代理浏览器到 3rd agent 容器内 Web 服务（HTTP + WebSocket）的访问，不侵入容器内服务。
>
> 参考实现：[jiuwenswarm PR #5500](https://gitcode.com/openJiuwen/jiuwenswarm/pull/5500)

---

## 1. 背景与目标

AgentOS Router 通过 YuanRong 创建和管理 3rd agent 容器。部分 agent（如 OpenClaw）在容器内提供 Web UI，需要从外部浏览器访问。

目标：

- 通用：任意 3rd agent，不限于 openclaw
- 双协议：同时代理 HTTP 与 WebSocket（含 SSE）
- SPA 兼容：相对路径、子资源、无尾斜杠 301
- 通过注入的 `web_resolver` 查询上游 URL，网关层透明代理
- 零侵入：不修改 agent 容器内服务

## 2. 整体架构

```
浏览器  --HTTP/WS-->  WebChannel(:19000)
                         |  /ws、/file-api 原样
                         |  /<agent_type>/[<path>]?user_id=<uid>&token=<iam>  → Web 代理
                         |  web_resolver(user_id, agent_type, protocol)
                         v
                   YuanRong frontend
                   /serverless/v1/http 或 /serverless/v1/ws
                         |
                         v
                   Agent 容器 Web 端口（openclaw 默认 18789）
```

入口 URL：

```
http://<gateway>:19000/<agent_type>/?user_id=<uid>&token=<iam>
ws://<gateway>:19000/<agent_type>/<path>?user_id=<uid>
```

首次由自有前端跳转打开。`auth_enabled` 时必须带 IAM token（`?token=` / `Authorization: Bearer` / `X-Token`）；成功后网关 `Set-Cookie: agentos_web_token`，OpenClaw 后续相对路径与 WebSocket 握手自动带 Cookie，不必再改 SPA。

## 3. 核心组件

### 3.1 Web 代理（挂在 WebChannel）

文件：`jiuwenswarm/gateway/channel_manager/protocol/web_proxy/web_proxy_connect.py`

与 `/file-api` 一样挂在 WebChannel FastAPI（默认 `:19000`），不另开端口。

- 路由 `/{agent_type}/{tail:path}` 与 `/{agent_type}` 注册在 `/ws`、`/file-api` 之后
- 保留路径 `ws`、`file-api` 等不进入代理
- `Upgrade: websocket` → WS 双向 pump，否则 HTTP 反向代理
- `_append_tail()`：YuanRong 风格 URL（含 query）追加 `path=/<tail>`；普通 URL 做路径拼接

### 3.2 WebResolver

由 `AgentOSRouter.resolve_web_endpoint` 注入：

`(user_id, agent_type, protocol) → upstream_url | None`

端口解析顺序：`metadata.web_port` → `metadata.agent_port` → openclaw 默认 `18789`。

OpenClaw 创建沙箱时会在 `rootfs.ports` 追加 `tcp:18789`，并写入 `metadata.web_port`。

### 3.3 配置

```yaml
channels:
  web_proxy:
    enabled: false          # 一体机模板默认 true；与 WebChannel 同端口
```

启用条件：`gateway.agent_client.type == agentos_router`，且 extension 提供 `resolve_web_endpoint`。

## 4. 鉴权

与 `/file-api` 共用 `authenticate_http`（IAM `POST /api/v1/auth/verify`）。

| `auth_enabled` | 行为 |
| --- | --- |
| `false` | 与原先一致：`user_id` 来自 query / Referer |
| `true` | 必须有合法 token；路由用 IAM `user_id`；query `user_id` 若出现必须与 token 身份一致 |

Token 来源（优先级）：

1. `?token=` / `Authorization: Bearer` / `X-Token`
2. Cookie `agentos_web_token`（首次跳转成功后下发，HttpOnly + SameSite=Lax + Path=/，Max-Age 900s，与 access_token TTL 对齐）
3. Referer 上的 `?token=`（Cookie 尚未落到的相对资源）

首次 301（补尾斜杠）会从 Location 去掉 `token`，改由 Cookie 续程，避免地址栏长期暴露凭证。IAM 头与 Cookie **不会**转发进容器。

前端跳转示例：

```
http://<gateway>:19000/openclaw/?user_id=<uid>&token=<access_token>
```

## 5. 错误码

| 状态码 | 场景 |
| --- | --- |
| 301 | 无 tail 的 HTTP 请求，重定向加 `/`（auth 开启时 Location 去掉 `token`） |
| 400 | 缺少 `agent_type` 或 `user_id`（含子资源 Referer 兜底后仍缺） |
| 401 | `auth_enabled` 且 token 缺失 / 无效 |
| 403 | query `user_id` 与 token 身份不一致 |
| 404 | resolver 返回 `None` |
| 502 | 上游连接错误 / 空 upstream URL |
| 503 | resolver 未配置 / channel 未完全启动 |
| 500 | 未处理异常 |

WebSocket 鉴权失败关闭码 `1008`。

## 6. 约束

1. 前提：`gateway.agent_client.type` 必须为 `agentos_router`
2. agent 需已通过 `3rdagent.switch` 创建
3. `auth_enabled` 时由自有前端在跳转 URL 上携带 IAM token；OpenClaw SPA 本身不感知 AgentOS 鉴权
4. 依赖项目已有的 `aiohttp`
