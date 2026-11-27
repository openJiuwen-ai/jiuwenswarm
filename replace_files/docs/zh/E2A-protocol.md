# E2A（Everything-to-Agent）协议说明

> **实现**：`jiuwenclaw/e2a/`（`models.py`、`adapters.py`、`constants.py`、`__init__.py`）。**版本**：`E2A_PROTOCOL_VERSION`（默认 `1.0`）。**冲突时**：以 `models.py` 中 dataclass 字段为最终实现源，并回头修正本文。请求：`E2AEnvelope`；响应：`E2AResponse`。  
> **English**：[../en/E2A-protocol.md](../en/E2A-protocol.md)

---

## 0. 文档位置与单一真源

| 位置 | 角色 |
|------|------|
| **docs/zh/E2A-protocol.md**（本文，中文） | 规范说明、易混点、示例 |
| **docs/en/E2A-protocol.md** | 同上，英文版 |
| `jiuwenclaw/e2a/models.py` | 请求 `E2AEnvelope`、响应 `E2AResponse` 与子结构；`from_dict` / `to_dict` |
| `jiuwenclaw/e2a/constants.py` | **`E2A_SOURCE_PROTOCOL_*`**、**`E2A_RESPONSE_KINDS`**、**`E2A_RESPONSE_STATUS_*`**、ACP 方法名与 SessionUpdate 字符串（运行时以元组为准） |
| `jiuwenclaw/e2a/adapters.py` | ACP / A2A → E2A；E2A → ACP JSON-RPC；**`E2AResponse` → ACP / A2A 投影**（见 §8） |
| `jiuwenclaw/e2a/__init__.py` | 对外导出 |
| `jiuwenclaw/e2a/ACP-reference.md` / `A2A-reference.md` | 外部协议阅读笔记，**非** E2A 规范性来源 |

---

## 1. 定位与边界

- **经 Gateway 规范化之后**发往 AgentServer 的载荷，用 E2A 表达；通道原始字段在 **Gateway 入口映射**到规范化字段（`user_id`、`message_id`、`params` 等），**不要求**把整包 channel JSON 当作 E2A 主路径。
- **`channel_context`**：可选；仅过渡或无法落入规范化字段的残余键；`from_dict` 可将旧 `metadata` 迁入以兼容历史。
- E2A **不规定**传输层（WebSocket、HTTP 等）。

---

## 2. 易混点：`method` 的两种含义（必读）

同一字段 **`method`** 在不同来源下含义不同，**不是**两套字段，也**不要**与 `request_id` / `message_id` 混用。

| 来源 | `method` 典型取值 | 说明 |
|------|-------------------|------|
| **网关 → AgentServer** | `chat.send`、`history.get`、`chat.interrupt`、… | 项目内 RPC 名；**不在** `constants.ACP_CLIENT_TO_AGENT_METHODS` 中枚举 |
| **ACP 经 `envelope_from_acp_jsonrpc` 转入** | `session/prompt`、`initialize`、… | 与 JSON-RPC `method` 一致；完整列表以 **`constants.ACP_CLIENT_TO_AGENT_METHODS`** 为准 |
| **内部特殊** | `null` | 例如心跳等无 RPC 枚举的场景 |

**出口转 ACP**：`envelope_to_acp_jsonrpc_call` 仅在 `method == "session/prompt"` 时经 `merge_params_to_acp_prompt` 从 **`params`** 补全 `prompt`；若当前 `method` 为 `chat.send`，需**另行**在业务适配层映射为 ACP 的 `session/prompt` 与 `params`，否则生成的 JSON-RPC 对纯 ACP 端无效。

---

## 3. 与 ACP、A2A 的关系（摘要）

| 维度 | ACP | A2A | E2A（对内） |
|------|-----|-----|-------------|
| 角色 | JSON-RPC 会话 | Task / Message / Card | 统一信封；外部经适配器**转入** |
| `method` | JSON-RPC method | 抽象操作（绑定层映射） | 见 **§2** |
| 扩展 | `_meta` | `metadata` | 网关用规范化字段；`a2a_metadata` / `acp_meta` 仅互操作 |

---

## 4. `E2AEnvelope` 字段表

### 4.1 基础与关联

| 字段 | 类型 | 说明 |
|------|------|------|
| `protocol_version` | string | 默认 `1.0` |
| `request_id` | string / null | 网关↔AgentServer 主 id（流式 chunk 对齐） |
| `jsonrpc_id` | string / number / null | ACP 转入时的 JSON-RPC `id` |
| `correlation_id` | string / null | 追踪链，与 `request_id` 分工不同 |
| `task_id` / `context_id` | string / null | A2A 对齐 |
| `session_id` | string / null | 会话 |
| `message_id` | string / null | 平台或 A2A 消息 id；语义勿与 `request_id` 混 |
| `is_stream` | boolean | 是否流式响应 |

### 4.2 `provenance`（`E2AProvenance`）

| 字段 | 说明 |
|------|------|
| `source_protocol` | `e2a` / `acp` / `a2a`（常量见 `E2A_SOURCE_PROTOCOL_*`） |
| `converter` / `converted_at` / `details` | 转换组件、时间、线索（如 `jsonrpc_method`） |

手工构造且未改默认时 `source_protocol` 为 `e2a`。旧字段 `binding` 会迁入 `provenance.details.migrated_from_binding`（兼容）。

### 4.3 时间、身份、入口

| 字段 | 说明 |
|------|------|
| `timestamp` | **规范**：RFC 3339 UTC 字符串；`from_dict` 可接受 float/int 纪元秒并规范化 |
| `identity_origin` | `system` / `user` / `agent` / `service` |
| `channel` | 入口名；**只写 `channel`**。`from_dict` 可读 `channel_id` |
| `user_id` / `source_agent_id` | 用户或上游 Agent |

`timestamp`（业务时刻）≠ `provenance.converted_at`（转入 E2A 时刻）。

### 4.4 `method`、`params`、桥接

| 字段 | 说明 |
|------|------|
| `method` | 见 **§2**；`from_dict` 可读 `req_method` |
| **`params`** | **唯一业务参数字典**（与 JSON-RPC `params` 对齐）：RPC 选项、用户正文、多模态块、附件等**全部**放在此对象内，**不再**使用顶层 `payload`（见下表约定） |
| `ext_method` | `method == "ext"` 时的真实名 |
| `session_update_kind` | 桥接 ACP `session/update` 时，与 `constants.ACP_SESSION_UPDATE_KINDS` 一致 |
| `expected_output_modes` | A2A `acceptedOutputModes` |

**`params` 常用键约定**（可按业务扩展，以下为与 ACP 桥接协作的推荐名）：

| 键 | 用途 |
|----|------|
| `prompt` | 已是 ACP ContentBlock 数组时直接使用（`merge_params_to_acp_prompt` 不覆盖） |
| `content_blocks` | 与 ACP `Vec<ContentBlock>` 同构的 JSON 数组；在 `method==session/prompt` 且无 `prompt` 时可映射为 `prompt` |
| `text` / `content` / `query` | 纯文本用户输入；无 `prompt`/`content_blocks` 时取第一个非空字符串生成单条 `text` block |
| `files` / `attachments` | 附件列表，元素形状见 **`E2AFileRef`**（`uri` 必填） |
| 其他 | 如 `mode`、`page_idx`、心跳文案等，随网关 RPC 自由扩展 |

### 4.5 `auth`、扩展槽

| 字段 | 说明 |
|------|------|
| `auth` | `E2AAuth`：凭据引用等 |
| `channel_context` | 可选溢出 / 旧 `metadata` 兼容，见 **§1** |
| `a2a_metadata` | **仅** A2A 互操作（与适配器参数名 `metadata` 对应） |
| `acp_meta` | 在 `merge_params_to_acp_prompt` 中合并入 `params._meta`（见 §7） |

---

## 5. 子结构（摘要）

- **`E2AFileRef`**：附件元素推荐形状（`uri` 必填；`name`、`mime_type`、`size`、`_meta` 可选）；通常出现在 **`params.files`** 或 **`params.attachments`** 数组中。
- **`E2AAuth`**：`method_id`、`bearer_token`、`api_key_ref`、`credential_ref`、`extra_headers`、`_meta`

---

## 6. `constants.py` 中的 ACP 字符串（桥接参考）

以下**仅**用于 ACP 桥接或文档对照，**运行时**以 `jiuwenclaw.e2a.constants` 中元组为准：

- **`ACP_CLIENT_TO_AGENT_METHODS`**：客户端 → Agent 的 JSON-RPC method 名
- **`ACP_AGENT_TO_CLIENT_METHODS`**、**`ACP_NOTIFICATION_NAMES`**：下行 / 通知
- **`ACP_SESSION_UPDATE_KINDS`**：`session_update_kind` 取值
- **`E2A_RESPONSE_KINDS`**、**`E2A_RESPONSE_STATUS_*`**、**`E2A_A2A_STREAM_BRANCHES`**：响应 `response_kind` / `status` / `a2a.stream_event.branch`（见 **§12**）

**网关 RPC**（`chat.send` 等）由项目自行约定，**不**放在上述元组中。

---

## 7. `merge_params_to_acp_prompt`

Python：`jiuwenclaw.e2a.merge_params_to_acp_prompt(envelope)`。

当且仅当 **`method == "session/prompt"`** 时：

1. 若 `params` 已有 `prompt`，不覆盖。
2. 否则若 `params.content_blocks` 为非空数组，用作 `prompt`。
3. 否则若 `params.text`、`params.content`、`params.query` 中有非空字符串，生成单条 `{ "type": "text", "text": "..." }` 作为 `prompt`。
4. 若信封有 `session_id` 且 `params` 无 `session_id`，则写入。
5. 将 `envelope.acp_meta` 合并进 `params._meta`。

其他 `method` 直接返回 `params` 的浅拷贝。

---

## 8. 适配器（`adapters.py`）

| 函数 | 作用 |
|------|------|
| `envelope_from_acp_jsonrpc` | ACP JSON-RPC → E2A，`provenance.source_protocol=acp`，`method` 为 ACP method |
| `envelope_from_a2a_send_message` | A2A SendMessage 语义 → E2A；参数 **`metadata`** 写入 **`a2a_metadata`**（A2A 专用，不是网关 `metadata`） |
| `envelope_to_acp_jsonrpc_call` | E2A → JSON-RPC 描述；不修改 `provenance` |
| `e2a_response_to_acp_jsonrpc_response` | **`E2AResponse`** → 单条 JSON-RPC **响应**对象（`result` 或 `error`）；不适用时返回 `None` |
| `e2a_response_to_a2a_stream_payload` | **`E2AResponse`**（`a2a.stream_event`）→ A2A **`StreamResponse`** 形 JSON（四选一）；不适用时返回 `None` |

---

## 9. 序列化与兼容键

- `E2AEnvelope.to_dict()` / `from_dict(data)`
- **只读兼容**：`channel_id`→`channel`，`req_method`→`method`，`metadata`→`channel_context`（仅当 `channel_context` 为空），`binding`→`provenance`（见上）
- **可选**：若 JSON 顶层仍存在旧键 **`payload`**（对象），其键会**合并进 `params`**（不覆盖 `params` 已有键），便于极少数遗留载荷；**新协议不应再发顶层 `payload`**。

---

## 10. 替代 `AgentRequest` 的字段对应

| AgentRequest | E2AEnvelope |
|--------------|-------------|
| `request_id` | `request_id` |
| `channel_id` | `channel` |
| `session_id` | `session_id` |
| `req_method` | `method` |
| `params` | `params` |
| `is_stream` | `is_stream` |
| `timestamp`（float） | `timestamp`（字符串，自动规范化） |
| `metadata` | 目标态：映射进规范化字段；兼容：`from_dict`→`channel_context` |

---

## 11. 示例（JSON 形状）

### 11.1 网关规范化后 → AgentServer（聊天，流式）

```json
{
  "protocol_version": "1.0",
  "request_id": "req_abc_01",
  "session_id": "sess_xyz",
  "channel": "web",
  "method": "chat.send",
  "is_stream": true,
  "timestamp": "2026-03-28T12:00:00+00:00",
  "identity_origin": "user",
  "user_id": "u_001",
  "params": {
    "content": "你好",
    "mode": "plan",
    "query": "你好"
  },
  "provenance": {
    "source_protocol": "e2a"
  }
}
```

`channel_context` 可省略（空对象序列化时可不出现或 `{}`）。

### 11.2 旧 `AgentRequest` 形状（`from_dict` 可读）

```json
{
  "request_id": "req_legacy_1",
  "channel_id": "feishu",
  "session_id": "sess_feishu_1",
  "req_method": "chat.send",
  "is_stream": true,
  "timestamp": 1774524781.15,
  "params": {
    "content": "查桌面文件",
    "mode": "plan",
    "query": "查桌面文件"
  },
  "metadata": {
    "feishu_open_id": "ou_xxx",
    "message_id": "om_xxx"
  }
}
```

解析后：`channel`=`feishu`，`method`=`chat.send`，`timestamp` 变为 RFC 3339 字符串；若未单独映射，`metadata` 会进入 `channel_context`（迁移期）。**目标态**应在 Gateway 把 `feishu_open_id` 等写入 `user_id` / `message_id` 等，并尽量清空 `channel_context`。

### 11.3 ACP 转入（逻辑示例，由 `envelope_from_acp_jsonrpc` 构造）

- `method` = `"session/prompt"`
- `jsonrpc_id` = `42`
- `params` = `{ "session_id": "s1", "prompt": [...] }`
- `provenance.source_protocol` = `"acp"`，`details` 含 `jsonrpc_method` 等

### 11.4 导出 ACP JSON-RPC（`envelope_to_acp_jsonrpc_call` 输出形状）

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "session/prompt",
  "params": {
    "session_id": "s1",
    "prompt": [{"type": "text", "text": "hi"}]
  }
}
```

仅当信封 `method == "session/prompt"` 且 `params` 中尚无 `prompt` 时，`merge_params_to_acp_prompt` 才会根据 `content_blocks` 或 `text`/`content`/`query` 补全 `prompt`。

### 11.5 `session/prompt` + 多模态（全在 `params`）

```json
{
  "method": "session/prompt",
  "session_id": "s1",
  "jsonrpc_id": 1,
  "params": {
    "content_blocks": [
      {"type": "text", "text": "请描述这张图"},
      {"type": "image", "mime_type": "image/png", "data": "<base64 omitted>"}
    ]
  }
}
```

经 `merge_params_to_acp_prompt` 后，`params` 会多出与 `content_blocks` 等价的 `prompt`（若原本无 `prompt`）。

---

## 12. E2A 响应协议（`E2AResponse`）

Agent → 网关 → 客户端的**每一条**出站记录（含流式多帧）使用 **`E2AResponse`** 表达，与 **`E2AEnvelope`** 字段命名对称（`snake_case`、RFC 3339 `timestamp`、复用 **`E2AProvenance`**）。

### 12.1 三层结构

| 层级 | 名称 | 说明 |
|------|------|------|
| **L1** | 响应信封 | 关联 id、顺序、流结束、状态、出处 |
| **L2** | 判别载荷 | **`response_kind`** + **`body`**（由 kind 约束） |
| **L3** | `projections`（可选） | `acp` / `a2a` 键下存放已组装的对外协议 JSON，**仅辅助**；业务解析**不得**依赖 L3 作为唯一真源 |

### 12.2 `is_final` 与 `status`

- **`is_final`**：`true` 表示同一 **`request_id`** 下**不再**会有后续 **`E2AResponse`** 记录。仅表示传输序列结束，**不**表示业务一定成功（失败帧也可 `is_final: true`）。
- **`status`**：取 **`constants.E2A_RESPONSE_STATUS_*`**（`succeeded` / `failed` / `in_progress`）。当 **`status == failed`** 时，**`response_kind`** 应为 **`e2a.error`**（若同时使用 `acp.jsonrpc_error` 作投影，以本文 **§12.6** 为准）。

### 12.3 不变量（规范）

- 同一 **`request_id`**：**`sequence`** 从 **0** 起严格递增（0, 1, 2, …）。
- 正常结束：**恰好一条**记录的 **`is_final`** 为 **`true`**。
- 异常（对端断连、进程崩溃）：可能**无法**发送 `is_final: true`；实现上应通过超时与连接状态补偿，本文不强制「必须补发结束帧」。

### 12.4 L1：信封字段表

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `protocol_version` | string | 是 | 与请求一致；默认 `1.0` |
| `response_id` | string | 是 | 本记录唯一 id（建议 UUID） |
| `request_id` | string | 是 | 回显请求；流式 chunk 对齐 |
| `sequence` | integer | 是 | 同 `request_id` 下从 0 递增 |
| `is_final` | boolean | 是 | 最后一帧为 `true` |
| `status` | string | 是 | `succeeded` / `failed` / `in_progress` |
| `response_kind` | string | 是 | 见 **§12.5**；运行时以 **`constants.E2A_RESPONSE_KINDS`** 为准 |
| `timestamp` | string | 是 | RFC 3339 UTC；本记录生成时刻 |
| `provenance` | object | 是 | **`E2AProvenance`**（与请求相同结构） |
| `body` | object | 是 | L2 载荷 |
| `jsonrpc_id` | string / number / null | 否 | 回显 ACP JSON-RPC `id` |
| `correlation_id` | string | 否 | 分布式追踪 |
| `task_id` / `context_id` / `session_id` / `message_id` | string | 否 | 与请求信封同义 |
| `is_stream` | boolean | 否 | 是否对应流式请求（回显或推断） |
| `identity_origin` | string | 否 | 默认倾向 **`agent`**（响应来自 Agent） |
| `channel` / `user_id` / `source_agent_id` | 同请求 | 否 | 回显 |
| `method` | string / null | 否 | 回显请求的 `method`（含心跳 `null`） |
| `metadata` | object | 否 | **通道/业务自定义**键值字典；与旧版 **`AgentResponse.metadata`** 对齐；**协议转换失败**时可临时写入兜底。**AgentServer→Gateway WebSocket** 整包旧 JSON 使用 **`constants.E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY`** / **`E2A_WIRE_LEGACY_AGENT_CHUNK_KEY`**（`_e2a_wire_legacy_*`），与业务自定义键建议用通道前缀区分 |
| `projections` | object | 否 | L3：`acp`、`a2a` 可选键，值为任意 JSON |
| `channel_context` / `a2a_metadata` / `acp_meta` | object | 否 | 与请求对称的溢出槽；**注意**：与 **`metadata`** 分工不同——`a2a_metadata` / `acp_meta` 仅 ACP/A2A 互操作，`metadata` 为通用业务与兜底 |

### 12.5 L2：`response_kind` 与 `body` 要点

运行时以 **`constants.E2A_RESPONSE_KINDS`** 为准；`body` 为 **dict**，下列为规范性说明。

| `response_kind` | `body` 要点 |
|-----------------|-------------|
| `e2a.complete` | **`result`**：业务结果对象（与请求的 **`params`** 哲学对称，避免无关键散落 L1） |
| `e2a.chunk` | **`delta_kind`**：`text` / `reasoning` / `tool` / `custom`；**`delta`**：字符串或结构化对象；可选 **`mime_type`** |
| `e2a.error` | **`code`**、**`message`**、**`details`**；可选 **`external`**：`{ "protocol": "acp"\|"a2a", "type": string, "payload": object }` |
| `acp.session_update` | 贴近 ACP `session/update` 通知负载；**`sessionUpdate`** 等字段与 **`ACP_SESSION_UPDATE_KINDS`** 对齐 |
| `acp.prompt_result` | 对应 `session/prompt` 的 JSON-RPC **`result`**（如 `stop_reason`、可选 `usage`、`_meta`） |
| `acp.jsonrpc_error` | 等价 JSON-RPC **`error`** 语义：`code`、`message`、可选 `data`（与 **`e2a.error`** 并存时：**内核**以 **`e2a.error`** 表达失败；**`acp.jsonrpc_error`** 可作 L3 或专用 kind 仅供 ACP 边界） |
| `a2a.task` | A2A **`Task`** 形对象；JSON 键名与官方 proto 的 JSON 映射一致（**snake_case**，与 `spec/a2a.proto` 字段对应） |
| `a2a.message` | A2A **`Message`** 形对象（同上） |
| `a2a.stream_event` | **`branch`**：`task` / `message` / `status_update` / `artifact_update`（常量 **`E2A_A2A_STREAM_BRANCHES`**）；**`payload`**：对应分支对象 |
| `ext` | **`ext_method`** + **`params`**（与请求 `ext` 对称） |

**推荐策略**：网关 / AgentServer **内部**优先使用 **`e2a.complete`** / **`e2a.chunk`** / **`e2a.error`**；在出口适配层生成 **`acp.*`** / **`a2a.*`** 或写入 **`projections`**。

### 12.6 与 ACP / A2A 的映射（摘要）

- **ACP**：JSON-RPC 响应由 **`jsonrpc_id`** + **`acp.prompt_result`**（`result`）或 **`e2a.error`** / **`acp.jsonrpc_error`**（`error`）映射；**`acp.session_update`** 对应 **`session/update`** 通知的 **`params`**。详见 **`e2a_response_to_acp_jsonrpc_response`**（§8）。
- **A2A**：**`a2a.stream_event`** 与 **`StreamResponse`** 四分支一一对应；**`e2a_response_to_a2a_stream_payload`** 产出单事件 JSON。 **`a2a.task`** / **`a2a.message`** 对应 SendMessage 两种返回。

### 12.7 日志建议（如 `E:\logs`）

- **一行一条**完整 **`E2AResponse`** JSON，便于检索与按 **`sequence`** 重放。
- 每条至少含 **`request_id`**、**`response_id`**、**`sequence`**、**`is_final`**、**`response_kind`**、**`timestamp`**。
- 勿记录 **`bearer_token`**、完整模型输出中的敏感内容；大 **`body`** 可将来扩展 **`body_ref`**（本版未定义）。

### 12.8 示例：流式两帧 + 结束帧

```json
{"protocol_version":"1.0","response_id":"r1","request_id":"req_1","sequence":0,"is_final":false,"status":"in_progress","response_kind":"e2a.chunk","timestamp":"2026-03-29T12:00:00+00:00","provenance":{"source_protocol":"e2a"},"body":{"delta_kind":"text","delta":"你好"}}
```

```json
{"protocol_version":"1.0","response_id":"r2","request_id":"req_1","sequence":1,"is_final":false,"status":"in_progress","response_kind":"e2a.chunk","timestamp":"2026-03-29T12:00:01+00:00","provenance":{"source_protocol":"e2a"},"body":{"delta_kind":"text","delta":"世界"}}
```

```json
{"protocol_version":"1.0","response_id":"r3","request_id":"req_1","sequence":2,"is_final":true,"status":"succeeded","response_kind":"e2a.complete","timestamp":"2026-03-29T12:00:02+00:00","provenance":{"source_protocol":"e2a"},"body":{"result":{"content":"你好世界"}}}
```

与 **`E2A-AgentRequest-log-migration.md`** 中的请求日志收编可对照：响应侧将来可同样约定统一前缀与单行 JSON。

---

## 13. 安全建议

- 优先 `credential_ref` / `api_key_ref`，由网关换票；日志勿完整打印 `bearer_token`。
- 跨域使用 TLS 与最小权限。

---

*本文与 `jiuwenclaw/e2a/models.py`（`E2AEnvelope`、`E2AResponse`）同步维护。*
