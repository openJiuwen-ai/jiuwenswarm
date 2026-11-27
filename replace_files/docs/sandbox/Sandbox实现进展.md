# Sandbox 实现进展

本文记录当前代码中已经落地的 Sandbox 相关改造进展，便于和目标设计文档、SandboxClient 接口文档对照。

> **DCS Key 速查**（含 Gateway 读写关系）：[DCS 项速查表](./DCS项速查表.md)

## 1. 当前结论

目前已经完成了不依赖沙箱反向建链细节的网关侧路由骨架：

- VibeSkill 创建会话时可以接收并保存 `user_id`。
- VibeSkill 后续转发给 Gateway / MessageHandler 的消息会携带 `user_id`。
- Gateway 在配置开启后直接使用 `SandboxRouterAgentClient` 作为 MessageHandler 的 agent client，不再创建默认 `WebSocketAgentServerClient`。
- Router 已按 `user_id` 维护 sandbox runtime、并实现并发创建保护、实例上限、FIFO 等待队列和 idle 回收。
- Router 本地维护远端沙箱 `expires_at`，在剩余时间低于 idle 阈值时自动 `refresh_duration`（§2.8）。
- 真实“沙箱内 Agent 主动连回 Gateway”的连接协议尚未实现，目前在 Router 的创建/删除流程中通过内部方法保留扩展点。

因此，当前阶段可以验证“identity 透传、session 映射、router 调度逻辑、明确失败路径”；还不能验证真实 sandbox agent 与 Gateway 建链后的端到端执行。

## 2. 已实现内容

### 2.1 VibeSkill session 与 user_id

`POST /api/v1/session` 已支持在 body 中传入 `user_id` 或 `userId`。

- 传入 `user_id` 时，写入当前 `VibeSkillSession.metadata["user_id"]`。
- 未传入时，使用创建出的 `session_id` 作为 fallback user key。
- SkillCreate 和 Standard 两种模式都使用同一套规则。
- `VibeSkillSessionStore` 新增 `get_user_id(internal_id)`，用于后续消息补充路由身份。

### 2.2 VibeSkill session 映射

`VibeSkillSessionStore` 当前只维护进程内 session 状态，不做落盘。内存中维护的内容包括：

- session 列表
- external/internal session 映射
- session state
- mode
- metadata
- created_at / updated_at

`get_or_create`、`bind_external`、`set_state`、`set_metadata`、`delete_session` 保持内存 API 行为。Gateway 重启后不能恢复 VibeSkill session 映射，调用方需要重新创建 session。

### 2.3 消息 identity 透传

VibeSkill 发往 MessageHandler 的 session 相关请求已经补充 `user_id`：

- SkillCreate：`skilldev.start`、`skilldev.parse_skill`、question reply、文件、版本、导出等请求。
- Standard：`chat.send` 以及创建 agent session 的路径。
- E2A normalize 路径保留并输出 `user_id`，Router 优先使用 `E2AEnvelope.user_id` 路由。

如果某条消息无法从 session store 找到 `user_id`，会 fallback 到 `session_id`，避免直接拒绝请求。

### 2.4 Sandbox Router 骨架

新增 `SandboxRouterAgentClient`，对 MessageHandler 暴露兼容 `AgentServerClient` 的接口：

- `send_request`
- `send_request_stream`
- `close`

Router 内部 runtime key 规则：

```text
有 user_id: vibeskill:user:{user_id}
无 user_id: vibeskill:session:{session_id}
```

Router 维护的 runtime 信息包括：

- `sandbox_id`
- `agent_client`
- `status`
- `task_count`
- `created_at`
- `last_active_at`
- `expires_at`（Gateway 本地维护的远端沙箱到期时刻，见 §2.8）
- `metadata`

当前 Router 负责：

- 同一 user key 复用同一个 sandbox runtime。
- 不同 user key 创建不同 sandbox runtime。
- per-key async lock，避免同一用户并发请求重复创建 sandbox。
- `max_sandboxes` 上限控制。
- FIFO 等待队列和 queue timeout。
- idle timeout 回收空闲 runtime。
- 远端沙箱存活续期（§2.8）：剩余时间低于 idle 阈值时调用 `refresh_duration`。
- 当容量已满且存在 IDLE 的沙箱 runtime 时，新请求会先触发 IDLE runtime 清理，再创建新 runtime。
- 当 runtime 结束任务进入 IDLE 且等待队列非空时，会立即清理该 runtime 并唤醒等待者。

### 2.5 SandboxClient 职责收敛

当前 Gateway 侧依赖 SandboxClient 的生命周期能力：

- `create_sandbox`
- `delete_sandbox`
- `refresh_duration`（续期远端存活，由 Router §2.8 调用）
- `close`

队列、上限、identity、runtime 复用、存活续期调度等逻辑都放在 Router 中，不放进 SandboxClient。

### 2.6 沙箱注册（DCS + init_data.json）

完整顺序：`create_sandbox` 成功 → DCS 写入 API Key 哈希 → 上传 `init_data.json` → OpenAbility 建链与通信（§2.7）。

`create_sandbox` 成功后，Router 在 `_register_sandbox_record` 中按顺序完成注册：

1. 向 DCS 写入：`key=jiuwen:sandboxApiKey:{sandbox_id}`，`value=API Key 的 SHA256`（明文 API Key 不落库）。
2. 向沙箱内上传 `init_data.json`（默认 `/opt/huawei/app/jiuwenclaw/init_data.json`），payload 为 `{"apiKey": "<Claw API Key>", "sandboxId": "<sandbox_id>"}`；经本地临时文件 + `SandboxClient.upload_file(...)` 写入。调用 `upload_file` 前会去掉任何目录前缀，只保留 basename 作为 `remote_path` 传入。
3. 任一步失败则 sandbox 注册失败，后续不会建 OA WebSocket。

实现：`jiuwenclaw/sandbox/sandbox_dcs_store.py`（DCS）、`jiuwenclaw/sandbox/sandbox_init_data.py`（`upload_sandbox_init_data`）；Router 入口：`SandboxRouterAgentClient._register_sandbox_record`。

沙箱内 AgentServer 通过环境变量 `SANDBOX_INIT_DATA_PATH` 指定读取路径（与 Gateway 侧默认路径一致）；未配置时使用 `/opt/huawei/app/jiuwenclaw/init_data.json`。

### 2.7 OpenAbility WebSocket 建链与通信

注册（DCS + `init_data.json`）完成后，Router 通过 **`OpenAbilityWebSocketClient`** 与 OpenAbility 建链并收发 E2A 业务消息。

**建链**（`_connect_open_ability_client`）：

1. 从 DCS `key=jiuwen:sandboxToOA:{sandbox_id}` 轮询读取 OpenAbility 地址，`value={ip}:{port}`。
2. 使用 `GATEWAY_TO_OA_WS_PATH` 拼出 `ws://{ip}:{port}{path}`，Gateway 主动连接 OpenAbility（无鉴权、无 `connection.ack` 首帧）；建连握手带 `x-hag-trace-id: jiuwen-gateway`。

**通信**（一条 WebSocket Text 帧 = 一次 JSON 载荷；经 OA 转发至沙箱内 AgentServer，内层仍为 E2A 线协议）：

- **Gateway → OA（出站）**：外层帧 `{"sandboxId": "sb-xxx", "msgDetail": "<json.dumps(E2A...)>"}`。
- **OA → Gateway（入站）**：裸 E2A JSON 字符串（原 `msgDetail` 字段值），Gateway 侧 `json.loads` 为 dict；无 `sandboxId`/`msgDetail` 外层。
- 入站内容与直连 AgentServer 的 E2A 线 JSON 一致，解析一次即可；出站多一层 `sandboxId` + `msgDetail` 封装。

实现：`jiuwenclaw/gateway/open_ability_wire.py`（wrap / parse_inbound）、`jiuwenclaw/gateway/open_ability_client.py`（建链、`send_request` / `send_request_stream`）。

```python
_connect_open_ability_client(sandbox_id, routing_key, metadata) -> OpenAbilityWebSocketClient
_disconnect_agent_client(sandbox_id, agent_client) -> None
```

### 2.8 远端沙箱存活续期

沙箱管理服务为每个实例设定 `SANDBOX_DURATION_SECONDS`（默认 3600s）存活上限，到期自动释放。为避免长会话（持续有任务、未达到 idle 回收）在 Duration 到期时被远端回收，Router 在 Gateway 侧本地维护剩余时间并主动续期。

**剩余时间维护（不 HTTP 查询）**

| 来源 | `expires_at` 计算 |
|------|-------------------|
| 本机 `create_sandbox` | `now + SANDBOX_DURATION_SECONDS` |
| DCS adopt | `routing.updated_at + SANDBOX_DURATION_SECONDS`（以 routing 写入时刻为创建锚点） |

续期成功后更新：`expires_at = now + SANDBOX_DURATION_SECONDS`。

**续期条件**

本地估算 `expires_at - now < SANDBOX_IDLE_TIMEOUT_SECONDS`（默认 600s）时，调用：

```text
refresh_duration(sandbox_id, duration_seconds=SANDBOX_DURATION_SECONDS)
```

每个 runtime 有独立 `asyncio.Lock`，避免并发重复续期。`TERMINATING` / `TERMINATED` 状态不续期。续期失败（异常或 API 拒绝）仅记日志，不阻断业务请求。

**触发时机**

| 时机 | 说明 |
|------|------|
| DCS adopt 完成后 | 新 Gateway 接管时，若估算剩余已低于阈值则立即续期 |
| `_idle_cleanup_loop` | 约每 `idle_check_interval_seconds`（30s）扫描所有非 TERMINATING runtime；**续期与 idle 回收独立**，续期不要求 `task_count == 0` |

**与 DCS TTL 的关系**：远端续期**成功**后，Gateway 同步刷新 `sandboxApiKey`、`sandboxToOA` 及（在 `SANDBOX_ADOPT_EXISTING=true` 时）`sandboxRouting` 的 TTL，并重写 routing 的 `updated_at`。`jiuwen:workspace:*` 与 `jiuwen:vibeskillSession:*` **不**随沙箱续期刷新。详见 [DCS 项速查表](./DCS项速查表.md)。

实现：`SandboxRouterAgentClient._maybe_refresh_sandbox_duration`、`_refresh_sandbox_dcs_ttl`（`jiuwenclaw/gateway/sandbox_router.py`）。

## 3. 配置与运行状态

通过环境变量 `SANDBOX_ENABLE=true/false` 开关沙箱路由。默认关闭，Gateway 使用原有 `WebSocketAgentServerClient`；开启时使用 `SandboxRouterAgentClient`。

路由相关环境变量：

| 变量 | 说明 | 默认 |
|------|------|------|
| `SANDBOX_ENABLE` | 是否开启沙箱路由 | `false` |
| `SANDBOX_MAX_NUM` | 同时存在的沙箱 runtime 上限 | `10` |
| `SANDBOX_MAX_QUEUE_SIZE` | 达上限时 FIFO 等待队列长度 | `100` |
| `SANDBOX_IDLE_TIMEOUT_SECONDS` | 空闲沙箱自动回收时间（秒）；**同时**作为远端存活续期触发阈值（剩余 `<` 此值时续期，见 §2.8） | `600` |
| `SANDBOX_DURATION_SECONDS` | 沙箱在远端的存活时长（秒），创建/续期时传给沙箱服务 | `3600` |
| `SANDBOX_TO_OA_REQUEST_TIMEOUT_SECONDS` | Gateway → OpenAbility 单次（非流式）请求应答超时（秒） | `600` |

固定：`queue_enabled=true`（始终排队）、`queue_timeout_seconds=60`、`idle_check_interval_seconds=30`。

开启时另需：`SANDBOX_API_BASE`、`SANDBOX_TEMPLATE_ID`、`SANDBOX_DCS_HOST`、`SANDBOX_DCS_PORT`、`SANDBOX_DCS_PASSWORD`、`GATEWAY_TO_OA_WS_PATH`。

可选：`SANDBOX_INIT_DATA_PATH` — 沙箱内 `init_data.json` 上传路径；Gateway 与 AgentServer 均读取此变量，默认 `/opt/huawei/app/jiuwenclaw/init_data.json`。Gateway 上传时会去掉任何目录前缀，仅以 basename 作为 `SandboxClient.upload_file` 的 `remote_path`。

DCS TTL（共用 `SANDBOX_DCS_HOST` 等连接配置，**各 key 默认 TTL 不同**）。完整 Key / Gateway 读写关系见 [DCS 项速查表](./DCS项速查表.md)。

| 变量 | 作用 key | 默认 |
|------|----------|------|
| `SESSION_DCS_TTL_SECONDS` | `jiuwen:vibeskillSession:*`、`jiuwen:workspace:*` | `0`（不过期） |
| `SANDBOX_DURATION_SECONDS` | 远端沙箱存活时长；亦作为下方 DCS 默认 TTL 的基准 | `3600` |
| `SANDBOX_DCS_TTL_SECONDS` | 显式覆盖 `sandboxApiKey` / `sandboxToOA` / routing 的 TTL；`0` 表示不过期 | 未设时：`sandboxApiKey` 与 routing 用 `SANDBOX_DURATION_SECONDS` |

可选（跨 Gateway failover）：

| 变量 | 说明 | 默认 |
|------|------|------|
| `SANDBOX_ADOPT_EXISTING` | 是否从 DCS 读取已有 `routing_key → sandbox_id` 并重连（Gateway 切换时复用沙箱） | `true` |
| `GATEWAY_INSTANCE_ID` | 写入 routing 映射的 Gateway 实例标识（日志/排障） | `hostname:pid` |

SandboxClient 固定：`timeout_seconds=120`，`metadata={}`。

OpenAbility 固定：`use_tls=false`，`connect_timeout_seconds=10`，`readiness_poll_interval_seconds=0.5`；reconnect 窗口默认 `reconnect_timeout_seconds=600`（`SANDBOX_OA_RECONNECT_TIMEOUT_SECONDS`），窗口内轮询 DCS + 重试 connect/probe。

### 3.1 跨 Gateway failover（routing 映射）

当 `SANDBOX_ADOPT_EXISTING=true` 时，`SandboxRouterAgentClient` 在本地 `_runtimes` miss 后会先查 DCS，再决定 **adopt 重连** 或 **create_sandbox**。

DCS Key（`jiuwenclaw/sandbox/sandbox_routing_dcs_store.py`）：

| Key | 值 |
|-----|----|
| `jiuwen:sandboxRouting:{routing_key}` | JSON：`sandbox_id`、`gateway_id`、`updated_at` |

`routing_key` 规则不变：`vibeskill:user:{user_id}`（优先）或 `vibeskill:session:{session_id}`。

**Acquire 顺序**（`_acquire_runtime_for_key`）：

1. 本地 `_runtimes` 命中 → 复用。
2. DCS `get_routing` 命中且 `jiuwen:sandboxToOA:{sandbox_id}` 可达 → **不** `create_sandbox`，仅建 OA WebSocket；若估算剩余低于 idle 阈值则续期（§2.8，含 DCS TTL 刷新）。
3. 映射存在但 OA 不可达 → 删 mapping，走创建。
4. 无映射 → `create_sandbox` → `SET NX` 写 mapping；NX 失败则删掉本机新建沙箱并 adopt 对端 `sandbox_id`。
5. 本地 idle/容量 `terminate` → 先删远端沙箱；成功后再删 `sandboxApiKey` / `sandboxToOA` 与 routing mapping（删沙箱失败则保留 DCS，避免映射丢失而沙箱仍存活）。

**运维前置**（与 VibeSkill 文档一致）：

- `SANDBOX_DCS_HOST`：读写 routing、`sandboxToOA`、`sandboxApiKey`、`workspace` 等（见 [DCS 项速查表](./DCS项速查表.md)）。
- WS 仍建议 LB 粘滞（流式上下文不跨 Gateway）；沙箱状态在沙箱进程内，与 WS 粘滞解耦。

**立即接管**：不等待租约；G1 假死而流量切到 G2 时，可能短暂双 Gateway 连同一 sandbox（需 OA/Agent 容忍重连）。

## 5. 已覆盖测试

当前已有单元测试覆盖：

- VibeSkill session store 内存状态与 `user_id` 映射。
- 创建 session 时保存 `user_id`，未传时 fallback 到 `session_id`。
- 删除 session 后内存映射同步删除。
- `message_to_e2a` 保留 `user_id`。
- 同一 `user_id` 复用 runtime。
- 不同 `user_id` 创建不同 runtime。
- 同一 `user_id` 并发请求只创建一次 sandbox。
- 达到 `max_sandboxes` 后进入 FIFO 队列。
- runtime 删除后队列出队。
- 容量已满时优先清理 IDLE runtime。
- runtime 进入 IDLE 且等待队列非空时立即清理。
- `task_count` 在成功、异常、流式取消路径回落。
- idle timeout 只回收 `task_count == 0` 的 runtime。
- 远端存活续期：剩余低于 idle 阈值时调用 `refresh_duration`；adopt 与 idle 巡检触发（`test_sandbox_router_duration_refresh`）。
- 沙箱注册时 DCS 写入后上传 `init_data.json`（`test_sandbox_router_init_data`）。
- `init_data.json` 序列化、路径与 `upload_file` 调用（`test_sandbox_init_data`、`test_sandbox_client_upload`）。
- 跨 Gateway：DCS adopt 不重跑 `create_sandbox`、stale mapping 清理、NX 竞争、terminate 删 mapping（`test_sandbox_router_failover`、`test_sandbox_routing_dcs_store`）。

可运行的目标测试：

```bash
uv run --no-sync pytest -o addopts='' \
  tests/unit_tests/channel/test_vibeskill_session_store.py \
  tests/unit_tests/gateway/test_sandbox_router_init_data.py \
  tests/unit_tests/gateway/test_sandbox_router_duration_refresh.py \
  tests/unit_tests/gateway/test_sandbox_router_failover.py \
  tests/unit_tests/sandbox/test_sandbox_routing_dcs_store.py \
  tests/unit_tests/sandbox/test_sandbox_init_data.py \
  tests/unit_tests/sandbox/test_sandbox_client_upload.py \
  tests/unit_tests/gateway/test_open_ability_wire.py \
  tests/unit_tests/gateway/test_open_ability_client.py \
  tests/unit_tests/e2a/test_gateway_normalize.py
```

## 6. 下一步

1. 在 Router 开启模式下，对接真实 OpenAbility + 沙箱 AgentServer，跑通 VibeSkill 端到端请求。
