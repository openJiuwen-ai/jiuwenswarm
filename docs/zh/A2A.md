# A2A 接入说明

本文说明 Gateway 侧 **A2A 入站服务**的管理入口、配置方式、与内部 `Message`/E2A 的对应关系及端到端验证方式；出站 A2A（Agent 调外部）见 §7。

> **运行时所有者**：`jiuwenswarm/gateway/a2a_manager/manager.py`（`A2AManager`）。**协议适配器**：`jiuwenswarm/gateway/channel_manager/protocol/a2a/a2a_connect.py`（`A2AChannel` + `a2a-sdk`）。**入口进程**：`python -m jiuwenswarm.gateway.app_gateway`。冲突时以源码为准，并回头修正本文。

---

## 0. 文档位置与单一真源

| 位置 | 角色 |
|------|------|
| **docs/zh/A2A.md**（本文） | 接入与开发联调：模块、配置、映射、验证 |
| `jiuwenswarm/gateway/a2a_manager/` | 入站配置、持久化、生命周期状态机与管理快照 |
| `jiuwenswarm/gateway/channel_manager/protocol/a2a/a2a_connect.py` | A2A HTTP 服务、`AgentCard`、请求/响应与 `Message` 互转 |
| `jiuwenswarm/gateway/app_gateway.py` | 组装 `A2AManager`，注册管理 API，并随 Gateway 启停 Manager |
| `jiuwenswarm/gateway/message_handler/message_handler.py` | 与 AgentServer 的 E2A 收发、内部 `Message` 编排 |
| `jiuwenswarm/gateway/channel_manager/channel_manager.py` | 频道注册与 `robot_messages` → `Channel.send` 派发 |
| [E2A-protocol.md](E2A-protocol.md) | Gateway↔AgentServer 内层协议 |

---

## 1. 职责边界

- **入站（本文）**：外部 A2A 客户端 → `A2AChannel` → `ChannelManager` → `MessageHandler` → E2A → AgentServer；回复沿同一路径返回，经 `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent` 输出（流式）或聚合结果（非流式）。
- **出站**：Agent 侧通过 A2A MCP Hub 等工具访问外部 A2A，接线在 AgentServer 适配层（见 §7），不在 `A2AChannel` 内实现。

---

## 2. 与 Web / ACP 通道的对照

| 项目 | Web | ACP | A2A（当前） |
|------|-----|-----|-------------|
| 绑定 | `WEB_HOST` / `WEB_PORT` / `WEB_PATH` | `ACP_GATEWAY_*` | `A2A_SERVER_*` |
| 配置来源 | 环境变量 + CLI（`--host` 等） | 仅环境变量 | `.env` 配置 + Web 管理 API |
| `.env` | `app_gateway` 启动时 `load_dotenv(get_env_file())`，即 `~/.jiuwenswarm/config/.env` | 同上 | 同上 |

---

## 3. 环境变量（Gateway）

在 `~/.jiuwenswarm/config/.env` 或进程环境中设置（`app_gateway.py` 读取）：

启用 A2A 前请先安装可选依赖：

```bash
pip install "jiuwenswarm[a2a]"
# 或（仓库/开发环境）
uv sync --extra a2a
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `A2A_SERVER_ENABLED` | 未设则关闭 | `1` / `true` / `yes` / `on` 为开启 |
| `A2A_SERVER_HOST` | `127.0.0.1` | HTTP 监听地址；对外服务常用 `0.0.0.0` |
| `A2A_SERVER_PORT` | `19100` | 与 Web、ACP 端口勿冲突 |
| `A2A_SERVER_PATH` | `/a2a` | JSON-RPC 入口路径 |
| `A2A_SERVER_PROTOCOL_VERSION` | `1.0.0` | 写入 `AgentCard` 的 `AgentInterface.protocol_version` |
| `A2A_SERVER_CARD_PATH` | `/.well-known/agent-card.json` | Agent Card 对外路径 |
| `A2A_SERVER_EXTENDED_CARD_PATH` | `/agent/authenticatedExtendedCard` | Extended Card 对外路径 |
| `A2A_SERVER_APP_NAME` | `JiuwenSwarm Gateway A2A Server` | Agent Card `name` |
| `A2A_SERVER_APP_DESCRIPTION` | `A2A ingress for JiuwenSwarm Gateway` | Agent Card `description` |
| `A2A_SERVER_APP_VERSION` | `0.1.0` | Agent Card `version` |
| `A2A_SERVER_EXPOSE_REASONING` | `true`（默认开启） | 开启后思考（reasoning）内容以 working 状态的 `TaskStatusUpdateEvent` 输出（见 §6.2）；设为 `false`/`0`/`no`/`off` 时直接丢弃 |
| `A2A_SERVER_AUTH_TYPE` | `none` | `none` / `bearer` / `api_key` |
| `A2A_SERVER_API_KEY_HEADER` | `X-API-Key` | API Key 使用的专用请求头名称 |
| `A2A_SERVER_CARD_AUTH_REQUIRED` | `false` | 公开 Agent Card 是否也要求认证 |
| `A2A_SERVER_API_KEY` | 空 | 安全凭据，沿用模型 API Key 的存储加解密机制 |

AgentServer 连接仍由网关既有逻辑配置（例如 `AGENT_SERVER_URL` 等），与 A2A 监听端口独立。

运行期间可在 Web 的“更多设置 → A2A 调度中心”查看状态、保存配置、启用、停用或重载入站服务，无需重启 Gateway。页面目前只管理入站监听，不提供外部 Agent 发现、出站调用或自动调度。

当 `A2A_SERVER_ENABLED=true` 且未安装 `jiuwenswarm[a2a]`（或 `uv sync --extra a2a`）时，Gateway 主流程仍会继续启动；A2A 通道启动失败会在日志中输出明确安装指引。

---

## 4. 对外端点

- **JSON-RPC**：`http://{A2A_SERVER_HOST}:{A2A_SERVER_PORT}{A2A_SERVER_PATH}`
- **Agent Card**：`http://{host}:{port}/.well-known/agent-card.json`（路径由 `A2AChannelConfig.card_path` 定义，默认 `/.well-known/agent-card.json`）

`AgentCard` 在 `A2AChannel.start()` 内构造：`supported_interfaces[0].url` 指向上述 JSON-RPC；`capabilities.streaming` 与技能列表见源码。

### 4.1 管理 API

Gateway Web HTTP 默认监听 `WEB_PORT + 2`（默认 `19002`），管理端点为：

| HTTP | 路径 | 作用 |
|------|------|------|
| `GET` | `/api/v1/a2a/ingress` | 读取目标配置、有效监听与运行状态 |
| `GET` | `/api/v1/a2a/ingress/history` | 读取入站请求处理历史；可通过 `limit` 查询参数读取最近 1–200 条 |
| `PATCH` | `/api/v1/a2a/ingress` | 保存配置；正文带 `apply: true` 时立即应用 |
| `POST` | `/api/v1/a2a/ingress:enable` | 持久化启用并启动监听 |
| `POST` | `/api/v1/a2a/ingress:disable` | 持久化停用并关闭监听 |
| `POST` | `/api/v1/a2a/ingress:reload` | 使用已保存配置重建监听 |

返回快照以 `desired_*` 表示持久化目标，以 `effective_*` 表示当前真实监听；异常时提供稳定错误码和可展示摘要。绑定 `0.0.0.0` 时页面会展示对外暴露告警。

### 4.2 安全凭据

在“入站配置 → 安全配置”选择 Bearer Token 或 API Key，输入或生成随机凭据。凭据要求 16–512 个可打印 ASCII 字符且不含空格；建议使用生成器产生的随机值。已保存凭据默认隐藏，点击输入框内的眼睛可查看或复制明文，刷新页面后仍可查看。存储沿用模型 API Key 的机制：配置了加密提供方时加密保存，否则保存原值。编辑界面通过专用 WebSocket 请求按需读取凭据；常规状态快照、HTTP GET 和失败响应不返回凭据，公开 Agent Card 不包含凭据或摘要，入站认证仅使用运行时摘要进行校验。

- Bearer：调用方发送 `Authorization: Bearer <凭据>`。
- API Key：调用方通过配置的专用请求头发送凭据，默认 `X-API-Key: <凭据>`。
- 启用认证后，所有 JSON-RPC 操作（含流式、查询、取消）及扩展 Card 都需要认证。“允许匿名查看服务信息”默认开启，关闭后查看公开 Agent Card 也需要认证。Card 中会公布认证方案，不包含凭据。
- 留空保留已有凭据；输入新值替换。要清除凭据，先选择“无认证”，再勾选清除。默认认证方式为无认证。
- 页面“保存”统一执行保存并应用，正在运行的入站服务会重启并使用新凭据；“取消”放弃未保存的修改并恢复已保存配置及凭据。

这是一组共享服务凭据，不提供调用方身份、逐任务访问隔离或 OAuth 登录。远程部署应通过 HTTPS 反向代理保护凭据传输。

---

## 5. 数据流（概要）

```mermaid
flowchart LR
    Caller["A2A 调用方"]
    Ch["A2AChannel"]
    CM["ChannelManager"]
    MH["MessageHandler"]
    AS["AgentServer E2A"]

    Caller --> Ch
    Ch --> CM
    CM --> MH
    MH --> AS
    AS --> MH
    MH --> CM
    CM --> Ch
    Ch --> Caller
```

入站将 A2A `message.parts` 映射为 `Message.params` 的 `query` 与可选 `files`；不写入 `params["a2a"]` 等扩展结构。回包将内部 `Message.payload` 映射为 A2A `Part` 列表（含多模态与工具事件文本化）。

---

## 6. 字段映射摘要

### 6.1 请求（A2A → `Message`）

| A2A / 上下文 | 内部 |
|--------------|------|
| `task_id` 或生成值 | `Message.id`（与回包关联） |
| `context_id` | `Message.session_id` |
| `parts[].text` | 合并为 `params.query` |
| `parts` 中非文本（url / data / raw） | `params.files[]`（含与 web 对齐的冗余键） |
| 元数据 | `Message.metadata` |

### 6.2 响应（`Message` → A2A）

| 内部 | A2A |
|------|-----|
| `payload.content`、工具相关事件等 | `Part(text=...)` 等，写入 `response` artifact |
| `payload.files[]` | `Part` 的 url / data / raw 等 |
| 思考内容（`chat.reasoning` 或 `chat.delta` 且 `source_chunk_type == "llm_reasoning"`） | 见下 |

**思考与正文的区分**：思考内容不会写入 `response` artifact。默认（`A2A_SERVER_EXPOSE_REASONING` 开启）以 working 状态的 `TaskStatusUpdateEvent` 输出，`status.message.parts[].metadata` 携带 `{"jiuwen_thought": true}` 标记（对齐 Google ADK 的 `adk_thought` 惯例），调用方可结构化识别或忽略；设为 `false`/`0`/`no`/`off` 时直接丢弃。

---

## 7. 出站 A2A（Agent 侧）

- 当前仓库未包含独立的 A2A MCP Hub 注册模块；若后续恢复该能力，请以实际接线代码与环境变量定义为准。

---

## 8. 端到端验证

仓库根目录的 [`demo/a2a_ingress_e2e.py`](../../demo/a2a_ingress_e2e.py) 使用真实 Gateway、AgentServer 和官方 `a2a-sdk`，验证热启停、改端口重载、Agent Card、`SendMessage` 与 `SendStreamingMessage`。先启动完整后端，再从仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe .\demo\a2a_ingress_e2e.py `
  --jsonl .\demo\a2a_ingress_e2e_result.jsonl
```

脚本会在结束时恢复运行前的 A2A 配置；多实例、端口覆盖和失败证据说明见 [`demo/README.md`](../../demo/README.md)。

---

## 9. 已知扩展点

- 鉴权、限流、超时与观测指标：由网关或前置代理统一补强时，保持 `A2AChannel` 只做协议与消息映射为宜。
- `jiuwenswarm/resources/.env.template` 未预置 A2A/ACP 键时，可在本地 `.env` 手工追加（与 §2 一致）。
