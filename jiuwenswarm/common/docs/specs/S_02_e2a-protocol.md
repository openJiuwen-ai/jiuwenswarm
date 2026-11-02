# E2A 统一信封协议

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / e2a-protocol |
| 关联模块 | jiuwenswarm/common/e2a/（含 acp/ 子目录，11 文件） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **E2A（Everything-to-Agent）统一信封协议**：外协议（ACP / A2A / Channel
Message）与内部 `AgentRequest` / `AgentResponse` 之间的双向转换、wire 上的编解码兜底、
以及 ACP 协议投影。内部数据模型的字段语义归 [[S_03]]；Gateway 侧的消息路由不归本 spec。

```
入站：ACP JSON-RPC / A2A send_message
  → adapters.envelope_from_*            （协议形状 → E2AEnvelope）
  → gateway_normalize.message_to_e2a_or_fallback   （系统边缘唯一正门）
  → agent_compat.e2a_to_agent_request    （E2AEnvelope → AgentRequest）
出站：AgentResponse / AgentResponseChunk
  → gateway_normalize.e2a_response_from_agent_chunk（5 条分派规则）
  → adapters.e2a_response_to_acp_jsonrpc_response / e2a_response_to_a2a_stream_payload（投影）
  → wire_codec.encode_agent_response_for_wire      （永不抛错的线编码）
对端解码：wire_codec.parse_agent_server_wire_unary/chunk（三层兜底）
```

## 不变量

- **N1 协议版本恒 `"1.0"`**：`E2A_PROTOCOL_VERSION`；wire 判定 `is_e2a_response_wire_dict`
  要求 `protocol_version == "1.0"` **且** `response_kind` 非空。版本升级属破坏性变更。
- **N2 边缘转换唯一正门**：外协议消息进系统只走
  `gateway_normalize.message_to_e2a_or_fallback`——**失败也产出合法 E2A 形状**（fallback
  信封），从不把裸异常抛出系统边界。Gateway 各 connect / handler 不得自行拼信封。
- **N3 wire 解码三层兜底顺序固定**：E2A dict → `metadata` 里的 legacy blob → 旧形状
  直读。不得增删层、不得乱序；每层的判定条件是下一层的前提。
- **N4 encode 永不抛错**：`encode_agent_response_for_wire` 任何输入失败都降级为可序列化
  的错误形状；`_json_safe` 负责 datetime→ISO、Enum→value、pydantic→model_dump。
- **N5 fallback 信封硬拒绝**：`agent_compat.e2a_to_agent_request` 遇 fallback 信封抛
  `RuntimeError`——fallback 形状只允许存在于"待归一"的中间态，不允许流入执行层。
- **N6 内部键过滤**：`E2A_WIRE_INTERNAL_METADATA_KEYS`（frozenset）所列键在信封出入站
  （from_dict / to wire）时一律剥离；`E2A_INTERNAL_CONTEXT_KEY = "_jiuwenswarm"` 是 E2A
  在 wire 上携带内部上下文的保留键。
- **N7 from_dict 宽容迁移**：`binding` → `provenance` 字段迁移；`params`/`payload` 合并
  **不覆盖**已有键；`sequence` 解析失败归 `0`。旧形状数据必须能无异常读入。
- **N8 ACP 能力自述保守**：`build_acp_initialize_result` 声明的能力是不支持
  loadSession / 图片 / 音频 / MCP http/sse——**宁可少报不可多报**；能力扩张需同步
  agentserver 的实际行为验证。
- **N9 messageId 会话内稳定**：`AcpSessionUpdateState` 保证同一逻辑消息（assistant /
  thought）跨帧 messageId 不变；增量去重 + 前缀延伸判定在 `build_acp_final_text_update`。
- **N10 HITL 特判**：`e2a_response_from_agent_chunk` 的 5 条分派规则中，HITL（人机交互
  中断）特判为 `is_final=False` + `in_progress`——**不视为终态**，等用户作答后续流。
- **N11 e2a 零 openjiuwen 依赖**：e2a 子包不 import openjiuwen / openjiuwen_runtime
  （纯标准库 + pydantic），保证信封模型可被任意边缘（含独立部署的协议网关）复用。
  `adapters.build_acp_tool_response_message` 对 `Message` 的依赖用惰性 import 规避。
- **N12 A2A 入站预留**：`envelope_from_a2a_send_message`（默认 `method="session/prompt"`）
  已实现但生产未接线——A2A 入站当前走 gateway 侧自有路径。接线时必须过 N2 正门约束。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `constants.py` | `E2A_SOURCE_PROTOCOL_*`、`E2A_RESPONSE_STATUS_*`、14 个 `E2A_RESPONSE_KIND_*`、`ACP_CLIENT_TO_AGENT_METHODS`（13）、`ACP_AGENT_TO_CLIENT_METHODS`（10）、`ACP_SESSION_UPDATE_KINDS`（12）、`FILE_TRANSFER_*` | 全部词法表的唯一定义点，e2a 内其它模块不得复写字面量 |
| `models.py` | `E2AEnvelope`（request_id / params / method / is_stream / agent_ref V2 透传）、`E2AResponse`（response_kind / status / sequence / is_final / projections）、`E2AProvenance`（source_protocol / converter / converted_at / details）、`IdentityOrigin`（SYSTEM/USER/AGENT/SERVICE；请求默认 USER、响应默认 AGENT）、`merge_params_to_acp_prompt` | dataclass；from_dict 见 N7 |
| `adapters.py` | `envelope_from_acp_jsonrpc` / `envelope_from_a2a_send_message` / `envelope_to_acp_jsonrpc_call` / `e2a_response_to_acp_jsonrpc_response`（不适用返回 None；错误码兜底 -32603）/ `e2a_response_to_a2a_stream_payload` / `build_acp_tool_response_message` | 外协议边缘的双向适配层 |
| `gateway_normalize.py` | `message_to_e2a_or_fallback`（N2 正门）、`e2a_from_agent_fields`（网关自发请求）、`e2a_response_from_agent_chunk`（5 条分派规则）、`e2a_response_to_agent_chunk`（逆向，含 cron / long_horizon / acp.output_request / plan.approval_required 映射） | `MAX_LEGACY_AGENT_REQUEST_JSON_BYTES = 512_000`：legacy blob 的尺寸上限 |
| `wire_codec.py` | `is_e2a_response_wire_dict`、`parse_agent_server_wire_unary` / `parse_agent_server_wire_chunk`、`encode_agent_response_for_wire`、`_json_safe` | N3 / N4 |
| `agent_compat.py` | `e2a_to_agent_request` | web 方向走 `web_routing_identity` + `apply_routing_metadata`（[[S_13]]）；非法 ReqMethod 抛 ValueError |
| `acp/protocol.py` | `build_acp_initialize_result` / `build_acp_session_new_result` / `build_acp_session_list_result` / `build_acp_prompt_result` | N8 |
| `acp/acp_tool_updates.py` | `normalize_tool_name`（9 别名表）/ `build_acp_tool_descriptor`（kind 推断词表 / title 生成 / locations 提取 `_PATH_KEYS`）/ `build_acp_tool_call_update` / `build_acp_tool_result_update`（status 推断链） | 工具名归一是 ACP 客户端兼容的单一真相源 |
| `acp/session_updates.py` | `AcpSessionUpdateState`（Protocol）/ `build_acp_session_update`（主分派器）/ `build_acp_final_text_update` / `build_acp_usage_update` | N9 |

## 数据结构

- `E2AEnvelope.params`：唯一业务字典（方法参数全在此）；`method` 对应 [[S_03]] 的
  ReqMethod 域。
- `E2AResponse.projections`：按目标协议缓存的投影结果（避免重复翻译）。
- `AcpSessionUpdateState`：跨帧状态（assistant_message_id / thought_message_id 等），
  由 ACP 连接会话持有，随连接生命周期清理。

## 消费者

- 入站装配：`gateway/`（message_handler、acp_connect、a2a_connect、agent_client、
  http_agent_client、cron、heartbeat、file_transfer_handler）。
- 出站装配：`server/`（agent_ws_server、wire_parse、ws_send、gateway_push、19 个 handlers）、
  `channels/acp`、`extensions/`（yuanrong、clawee、agentos）、`agents/harness`
  （acp_output_tools、interrupt_helpers）。

## 修改检查单

- 改信封字段 / 加字段 → `from_dict` 迁移路径（N7）+ `E2A_WIRE_INTERNAL_METADATA_KEYS`
  是否需要扩 + [[S_03]] AgentRequest 映射同步。
- 改 response_kind 枚举 → `constants.py` 词表 + `wire_codec` 判定 + gateway/server 两侧
  分派规则逐个确认。
- 改 ACP session update 生成 → `tests/unit_tests/e2a/test_acp_session_updates.py` +
  messageId 稳定性（N9）+ 前端增量渲染兼容。
- 新增外协议接入 → 先走 N2 正门评估，禁止绕过 `message_to_e2a_or_fallback` 自拼信封。
- 动 wire 格式 → 三层兜底（N3）各层回归 + 双进程（gateway/server）版本错位场景测试。

## 与其它 spec 的关系

- [[S_03]]：AgentRequest / Message 的字段语义（本 spec 只管转换边界）。
- [[S_13]]：路由身份四字段（`agent_compat` web 方向消费）。
- [[S_11]]：文件传输的 E2A kind（`FILE_TRANSFER_*` 常量）。
