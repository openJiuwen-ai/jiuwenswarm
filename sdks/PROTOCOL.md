# Process CLI SDK 协议 0.1，修订 1

这是 JiuwenSwarm 自身的本地进程协议。Python/TypeScript SDK 使用 stdin/stdout 管道调用 CLI，每次调用启动一个子进程，收到终态并等待进程退出。协议不依赖 Gateway 或网络服务。

完整机器可读契约位于 [schema.json](../jiuwenswarm/channels/process_cli/protocol/schema.json)。它包括 run、query、answer、tool_result、cancel、event、result、query_result，以及目录查询数据和关键事件载荷。使用 JSON Schema Draft 2020-12 校验单条记录；另外校验 UTF-8、JSONL 分帧、sequence、请求/会话身份和 OS 退出码。Schema 文件会随 CLI 安装包分发。

## 1. 接入前查询协议能力

先发送以下 `capabilities.json`：

```json
{"schema_version":"0.1","type":"query","request_id":"capabilities-1","operation":"protocol.capabilities","params":{}}
```

```powershell
jiuwenswarm-process --query-json .\capabilities.json
```

最终 `query_result.data` 包含：

- `schema_version`、`supported_schema_versions`：外层记录格式与支持的版本。
- `protocol_revision`：兼容扩展的修订号；本次为 `1`。
- `features`：当前可执行文件已经实现的功能，例如 `empty_tools`、`stable_event_fields`、`field_errors`、`host_tools`。
- `run_fields`、`query_operations`、`control_types`、`stable_event_types`：可接受的字段、操作与已固定载荷的事件类型。
- 编码、输入字节限制和兼容规则。

能力声明表示 CLI 实现支持；模型、Skill、MCP 的实际可用性仍由配置和 Runtime 决定。模型用 `model.list`/`model.resolve` 查询，MCP 用 `mcp.validate` 查询。

获取该可执行文件对应的完整 Schema：

```json
{"schema_version":"0.1","type":"query","request_id":"schema-1","operation":"protocol.schema","params":{}}
```

```powershell
jiuwenswarm-process --query-json .\schema.json
```

响应仍为一个 `query_result`，完整 Schema 在 `data`，不是裸 Schema 文件。这两个协议查询不创建 Runtime、会话或 Agent，也不需要模型配置。

SDK 调用：

```python
capabilities = await client.query("protocol.capabilities")
if capabilities["status"] != "completed":
    raise RuntimeError("CLI does not implement protocol discovery")
if not capabilities["data"]["features"].get("empty_tools", False):
    raise RuntimeError("CLI cannot disable all tools")
schema = (await client.query("protocol.schema"))["data"]
```

```typescript
const capabilities = await client.query("protocol.capabilities");
const schema = await client.query("protocol.schema");
```

## 2. 版本与兼容规则

1. `schema_version` 固定外层格式、现有字段含义和已声明的公共载荷。不自动协商或猜测未支持的版本；不支持的版本返回 `INVALID_INPUT`。
2. 增加可选输入能力、输出字段或观察事件可以保留 `0.1`，同时更新修订号和能力清单。宿主先查能力再发送可选字段；修订号本身不代替功能判断。
3. 删除/重命名字段、修改字段类型或改变已有语义需要新 `schema_version`，并明确保留旧版本的支持期限。
4. run、query 和控制消息的外层及配置对象拒绝未知字段，避免拼写错误被静默忽略。`answers` 中的对象保留历史 JSON 扩展字段，公开字段仍须符合 Schema。输出消费者忽略未知字段、未知观察事件和未知错误码；未知错误码仍表示失败。
5. 不支持能力查询的旧 CLI 不具备本修订的承诺。宿主可按其明确支持的旧功能运行，或者报告版本/功能不匹配，不应发送未确认的配置。
6. `chat.final`、EOF 和 ACK 都不是进程成功标志。成功需要唯一终态 `result`/`query_result`，正确请求与会话身份，连续 sequence，以及一致的 OS 退出码。强制终止或断管道可能没有终态，SDK 报传输/协议错误。

新增公共事件字段保留原始载荷字段；现有读取 `payload.interaction` 的宿主继续可用。新增宿主使用下节的固定字段。

## 3. 配置优先级、默认值与空值

| 字段 | 省略或 `null` | 显式值与优先级 |
|---|---|---|
| `request_id` | CLI/SDK 生成本次请求 ID | 非空字符串，贯穿输出与控制；不是幂等重试键 |
| `session_id` | 新建会话 | 恢复已有会话；调用仍启动新进程 |
| `mode` | 新会话 `agent.code.normal`；恢复会话继承已绑定模式 | 四种单 Agent 模式；不支持 Team/Workflow 根入口 |
| `agent` | 使用配置的默认 Agent；已绑定自定义 Agent 的会话不能省略其定义 | 恢复时重发相同定义，否则返回定义冲突；不在续接时静默更换 Agent |
| `agent.name` | 必填，不能为 `null` | 3–50 个 ASCII 字母、数字、下划线或连字符；同名不同定义不可静默替换 |
| `agent.description` | 无附加描述 | 非空字符串，作为 Agent 的说明 |
| `agent.instructions` | 必填，不能为 `null` | 附加到 Runtime 的 Agent 身份/系统规则；不替换权限策略 |
| 顶层 `model` | 使用 `agent.model`，再使用 Runtime/会话默认选择 | 顶层显式模型优先；两处提供的模型引用都会校验 |
| `agent.model` | Runtime 默认选择 | Agent 默认模型，低于顶层按次选择 |
| `agent.tools` | 省略为 `["*"]`；`null` 非法 | `[]` 禁用全部工具；`["*"]` 使用配置工具；普通数组为白名单，不可与 `*` 混用 |
| `agent.skills` | 省略为 `[]`；`null` 非法 | 构造 Agent 时传入的基础 Skill 声明 |
| 顶层 `skills` | 不设置本次显式 Skill 选择 | `[]` 不附加本次选择；数组按本次请求传给 Runtime。它不等于禁用 Runtime 的全部 Skill 检索或清空 `agent.skills` |
| 顶层 `mcp` | 不设置本次显式 MCP 选择 | `[]` 不显式选择 MCP；普通数组选择已配置引用，不会断开全局连接或改变其他 channel；Runtime 内建配置仍适用 |
| `permissions` | 继承 Runtime 权限配置 | `tools` 中按工具设置 allow/ask/deny；未指定工具继承原策略，全局 deny 仍优先 |
| `permissions.tools: {}` | 不增加覆盖 | 空对象与未设置覆盖等效；`null` 非法 |
| `host_tools` | 省略为 `[]`；`null` 非法 | 声明宿主回调工具；还须通过 `agent.tools` 白名单与权限检查，声明不自动授权；SDK 需要工具回调 |
| `output_schema` | 不校验结构化结果 | 根类型必须为 object；只把符合 Schema 的最终回答放入 `output_json`，不匹配则失败 |
| `max_turns` | 不添加本次模型调用上限 | 1–1000；按模型调用计数，工具调用不是一 turn |
| `agent.max_iterations` | Runtime 的 Agent 迭代上限 | 与 `max_turns` 同时存在时使用更严格的限制；旧迭代限制先触发时仍可能为 Runtime 迭代错误 |
| `max_budget_usd` | 不添加本次费用上限 | 正数 USD；按调用报告的费用累计，缺少可用计费数据则失败；一次调用可能先越界再停止 |
| `timeout_seconds` | 不添加 CLI 执行超时 | 正数，覆盖 Runtime 执行；SDK 宿主 deadline 另外覆盖启动、I/O 和退出等待 |
| `workspace` | 新会话使用当前进程目录；恢复继承持久化绑定 | 部分字段未设置时保留相应默认/绑定；路径由 CLI 解析 |
| `workspace.trusted_dirs` | 省略为 `[]`；`null` 非法 | `[]` 不附加信任目录；不撤销平台已有的信任根 |

`agent.tools: []` 不会被当作“使用默认工具”；模型发现与执行边界均拒绝所有工具，包括后来由 rail、Skill 或 MCP 注册的工具。正常使用配置能力的 Agent 保持原行为。

## 4. 固定的公共事件字段

事件外层始终包含 `schema_version`、`type:"event"`、`sequence`、`request_id`、`session_id`、`event_type`、`payload`。sequence 从 0 连续递增；一个请求内保持同一请求 ID，解析后保持同一会话 ID。

| `event_type` | 必须可读取的 `payload` 字段 |
|---|---|
| `chat.delta` | `text`：本次文本增量；按顺序追加 |
| `chat.final` | `text`：最终回答段；仍不是进程终态，不要与全部增量重复拼接 |
| `chat.tool_call` | `tool.call_id`、`tool.name`、`tool.arguments`、`tool.status:"started"`、`tool.result:null` |
| `chat.tool_result` | `tool.call_id`、`tool.name`、`tool.arguments`、`tool.status:"completed"/"failed"`、`tool.result` |
| `interaction.requested` | `interaction_id`、`kind:"permission"/"question"/"confirmation"`、`questions` |
| `host_tool.requested` | `call_id`、`name`、`arguments`，由宿主回调处理 |

工具观察的参数无法解析时 `tool.arguments` 为 `null`；无法取得观察 ID/名称时为空字符串，不应据此发控制消息。只有 `host_tool.requested` 的 `call_id` 可用于 `tool_result`。

`questions` 每项具有 `question_id`（此交互内的 q0/q1）、`question`、`options:[{value,label}]`、`card_id`（无卡片时 null）、`allow_custom_input`。value 是审批或选项的实际值，label 只用于显示。`question_id` 用于显示关联；回答按 questions 顺序提交，不能只传 question_id。

`payload.interaction` 是保留的旧载荷。新宿主不需要依赖其中的 Runtime source、request_id 或其他内部字段；SDK 自动携带必要的进程身份和交互 ID。

## 5. 审批与工具回调的完整控制记录

CLI 发出审批事件（省略其他观察事件）：

```json
{"schema_version":"0.1","type":"event","sequence":12,"request_id":"run-1","session_id":"session-1","event_type":"interaction.requested","payload":{"interaction_id":"opaque-approval-id","kind":"permission","questions":[{"question_id":"q0","question":"允许写入报告吗？","options":[{"value":"allow_once","label":"允许一次"},{"value":"reject","label":"拒绝"}],"card_id":"original-card-id","allow_custom_input":false}],"interaction":{}}}
```

宿主获得决定后，将以下单行 JSON 写入同一子进程 stdin。批准一次示例：

```json
{"schema_version":"0.1","type":"answer","request_id":"run-1","session_id":"session-1","interaction_id":"opaque-approval-id","answers":[{"question":"允许写入报告吗？","selected_options":["allow_once"],"custom_input":"","card_id":"original-card-id"}]}
```

拒绝使用事件中提供的 `reject` 值。权限卡复制原始 card_id；普通问题没有 card_id 时省略。Python `on_interaction` / TypeScript `onInteraction` 只返回上述 answers 数组，SDK 自动组装外层。无回调时 SDK 只自动拒绝权限请求；普通提问仍需要宿主回答，否则取消并报 `InteractionRequired`。

宿主工具结果：

```json
{"schema_version":"0.1","type":"tool_result","request_id":"run-1","session_id":"session-1","call_id":"opaque-tool-id","result":{"available":17}}
```

失败时把 `result` 换成 `error:"Lookup failed"`，两者只能出现一个。Python `on_tool_call` / TypeScript `onToolCall` 返回 JSON 值，SDK 组装控制消息；回调异常作为工具错误发送。

取消：

```json
{"schema_version":"0.1","type":"cancel","request_id":"run-1","session_id":"session-1"}
```

每个 `--run-jsonl` 子进程只接受首次 run 与后续 answer/tool_result/cancel，不接受第二个 run。下一轮用新进程和相同 session_id 续接。

## 6. 最终结果和字段错误

运行结果包含 `status`、`exit_code`、`output`、`output_json`、`error`、`usage`。查询使用独立的 `query_result`，业务数据在 `data`。

| status | exit_code | 含义 |
|---|---|---|
| completed | 0 | 正常结束，error 为 null |
| failed | 1 | 执行失败或上限错误 |
| failed | 2 | 输入非法 |
| timed_out | 124 | 执行超时 |
| cancelled | 130 | 调用者取消或输入控制中断 |

`output` 为可展示的文本，可能包含工具前的说明；结构化业务消费读取 `output_json`。结构化校验针对最后一次工具调用之后的完整回答段；不能从任意散文中截取一个 JSON 片段冒充成功。

`usage` 是本次运行的统计，不是上下文占用。已知字段包括 input_tokens、output_tokens、total_tokens、cache_tokens、input_cost、output_cost、total_cost（USD）及启用限制时的 model_calls。可缺少未报告的字段；缺失不等于 0。额外统计字段可忽略。

输入错误保留 `INVALID_INPUT` 和退出码 2，在 `error.details` 给出字段路径，例如：

```json
{"code":"INVALID_INPUT","message":"run input does not match the schema","retryable":false,"details":{"field":"/agent/tools/0","reason":"invalid_type","expected":"string"}}
```

field 使用 JSON Pointer；reason 为 invalid_type、invalid_value、required 或 unknown_field。未知输入字段只定位到其所属对象，不回显未声明字段名或输入值。无法从无效 JSON 定位字段时仍返回通用输入错误。控制错误使用 INVALID_CONTROL；无效输出 Schema 使用 INVALID_OUTPUT_SCHEMA 并定位 `/output_schema`。

## 7. Schema 维护与验收

```powershell
python .\scripts\generate_process_cli_schema.py
python -m pytest -o addopts='' tests/unit_tests/process_cli/test_public_contract.py
```

Schema 是生成产物，生成器与产物一致性受测试约束。真实模型验收另见 `tests/system_tests/test_process_cli_protocol_revision_live.py`，需明确设置 `JIUWENSWARM_REAL_MODEL_TEST=1` 并配置本地模型。
