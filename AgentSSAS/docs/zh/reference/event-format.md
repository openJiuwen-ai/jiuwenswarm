# 事件格式参考

AgentSSASSecurityRail 采集的原始事件（raw_event）采用三层结构：`common`（公共关联字段）/ `payload`（事件内容）/ `metadata`（预留扩展）。本文给出三层结构总览、common 层 14 个字段的逐一说明、7 类事件的清单与完整 JSON 示例。

相关文档：

- [检测模块参考](./detection-modules.md)：检测模块如何订阅这些事件
- [HTTP API 参考](./http-api.md)：通过 HTTP 上报 raw_event 的方式
- [查看威胁日志](../how-to/view-threat-logs.md)：事件落盘后的查询方式

## 三层结构总览

```text
raw_event
├── common     # 公共关联字段(14 个):标识事件来源、类型与在会话/交互/调用链中的位置
├── payload    # 事件内容:按事件类型携带输入输出或安全检测结果
└── metadata   # 预留扩展层,当前为空 dict
```

| 层 | 职责 | 备注 |
|----|------|------|
| `common` | 事件路由与关联所需的全部标识字段 | 结构对所有事件类型固定 |
| `payload` | 事件的具体内容 | 生命周期事件含 `content` 键；工具事件额外含 `tool_name`、`tool_call_id`；安全检测事件含 `risk_*` 字段 |
| `metadata` | 预留扩展 | 当前固定为空对象 |

## common 层字段（14 个）

| 字段 | 类型 | 说明 |
|------|------|------|
| `source` | str | 上报源标识，如 `AgentSSASSecurityRail` |
| `event_type` | str | 事件类型，见下文 7 类事件清单 |
| `event_class` | str | 事件大类：`lifecycle`（生命周期事件）/ `security`（安全检测衍生事件） |
| `timestamp` | float | 事件时间戳（Unix 秒） |
| `interaction_seq` | int | 交互轮次序号；一次用户请求到最终响应的完整过程为一个交互，int 类型自增序号 |
| `session_id` | str | 会话唯一标识 |
| `conversation_id` | str | 会话展示 ID，通常与 session_id 一致 |
| `agent_id` | str | 智能体 ID |
| `trace_id` | str | 链路追踪 ID，贯穿一次完整追踪 |
| `context_id` | str | 上下文 ID |
| `llm_call_seq` | int | LLM 调用序号；llm_call 事件时为本次大模型调用的序号，tool_call 事件时为所属大模型调用的序号；不具备时为 -1 |
| `tool_call_seq` | int | 工具调用自增序号；非工具事件为 -1 |
| `subsession_id` | str | 子会话 ID |
| `tool_call_id` | str | 上报端工具调用 ID；用于跨 tool_input / tool_output 事件显式关联同一工具调用 |

序号字段（`interaction_seq`、`llm_call_seq`、`tool_call_seq`）的约定：缺省值为 `-1`，表示当前事件不具备此序号；首个有效值为 `0`，此后自增。

关于时间字段的落库存储：raw_event 中的 `timestamp`（Unix 浮点秒）写入数据库（events / alerts / raw_events 三表）时，会同时写入 `timestamp_text` 可读列（本地时区 `YYYY-MM-DD HH:MM:SS.mmm` 格式），便于直接查看；`timestamp` 列仍为 Unix 浮点秒，用于计算与排序。旧库打开时自动迁移补列并回填历史数据。

## 事件清单（7 类）

| 事件类型 | event_class | payload 关键字段 | 说明 |
|---------|------------|-----------------|------|
| `invoke_start` | lifecycle | `content.query` | 交互开始，用户输入 |
| `llm_input` | lifecycle | `content.messages` | LLM 调用输入（prompt） |
| `tool_input` | lifecycle | `tool_name`、`tool_call_id`、`content.tool_args` | 工具调用输入（参数） |
| `tool_output` | lifecycle | `tool_name`、`tool_call_id`、`content.tool_result` | 工具调用输出（结果） |
| `llm_output` | lifecycle | `content.response` | LLM 调用输出（响应） |
| `invoke_end` | lifecycle | `content.result` | 交互结束，最终结果 |
| `permission_interrupt_tool` | security | `risk_source`、`risk_type`、`risk_level`、`decision`、`evidence`（另含 `tool_name`、`tool_call_id`、`content`） | 安全检测衍生事件：其他安全 Rail 拦截了危险工具调用 |

一次完整交互的事件流顺序为：

```text
invoke_start -> llm_input -> tool_input -> tool_output -> llm_output -> invoke_end
```

（无工具调用的交互中，`tool_input` / `tool_output` 不出现；多次工具调用时相应事件可成对出现多组。）

## 各类事件的完整示例

以下示例与 `tests/fixtures/event_factory.py`（虚拟 raw_event 生成器）产出的结构一致。`metadata` 层当前恒为空对象，示例中省略展示的差异仅为取值本身。

### invoke_start

```json
{
  "common": {
    "source": "AgentSSASSecurityRail",
    "event_type": "invoke_start",
    "event_class": "lifecycle",
    "timestamp": 1767196800.0,
    "interaction_seq": 0,
    "session_id": "test-session",
    "conversation_id": "test-session",
    "agent_id": "test-agent",
    "trace_id": "test-trace",
    "context_id": "test-ctx",
    "llm_call_seq": -1,
    "tool_call_seq": -1,
    "subsession_id": "",
    "tool_call_id": ""
  },
  "payload": {
    "content": { "query": "帮我列出当前目录下的文件" }
  },
  "metadata": {}
}
```

### llm_input

```json
{
  "common": {
    "source": "AgentSSASSecurityRail",
    "event_type": "llm_input",
    "event_class": "lifecycle",
    "timestamp": 1767196801.0,
    "interaction_seq": 0,
    "session_id": "test-session",
    "conversation_id": "test-session",
    "agent_id": "test-agent",
    "trace_id": "test-trace",
    "context_id": "test-ctx",
    "llm_call_seq": 0,
    "tool_call_seq": -1,
    "subsession_id": "",
    "tool_call_id": ""
  },
  "payload": {
    "content": {
      "messages": [
        { "role": "user", "content": "帮我列出当前目录下的文件" }
      ]
    }
  },
  "metadata": {}
}
```

### tool_input

```json
{
  "common": {
    "source": "AgentSSASSecurityRail",
    "event_type": "tool_input",
    "event_class": "lifecycle",
    "timestamp": 1767196802.0,
    "interaction_seq": 0,
    "session_id": "test-session",
    "conversation_id": "test-session",
    "agent_id": "test-agent",
    "trace_id": "test-trace",
    "context_id": "test-ctx",
    "llm_call_seq": 0,
    "tool_call_seq": 0,
    "subsession_id": "",
    "tool_call_id": "call-001"
  },
  "payload": {
    "tool_name": "bash",
    "tool_call_id": "call-001",
    "content": { "tool_args": { "command": "ls" } }
  },
  "metadata": {}
}
```

### tool_output

```json
{
  "common": {
    "source": "AgentSSASSecurityRail",
    "event_type": "tool_output",
    "event_class": "lifecycle",
    "timestamp": 1767196803.0,
    "interaction_seq": 0,
    "session_id": "test-session",
    "conversation_id": "test-session",
    "agent_id": "test-agent",
    "trace_id": "test-trace",
    "context_id": "test-ctx",
    "llm_call_seq": 0,
    "tool_call_seq": 0,
    "subsession_id": "",
    "tool_call_id": "call-001"
  },
  "payload": {
    "tool_name": "bash",
    "tool_call_id": "call-001",
    "content": { "tool_result": "a.txt\nb.txt" }
  },
  "metadata": {}
}
```

### llm_output

```json
{
  "common": {
    "source": "AgentSSASSecurityRail",
    "event_type": "llm_output",
    "event_class": "lifecycle",
    "timestamp": 1767196804.0,
    "interaction_seq": 0,
    "session_id": "test-session",
    "conversation_id": "test-session",
    "agent_id": "test-agent",
    "trace_id": "test-trace",
    "context_id": "test-ctx",
    "llm_call_seq": 0,
    "tool_call_seq": -1,
    "subsession_id": "",
    "tool_call_id": ""
  },
  "payload": {
    "content": { "response": "我将调用 bash 工具列出文件" }
  },
  "metadata": {}
}
```

### invoke_end

```json
{
  "common": {
    "source": "AgentSSASSecurityRail",
    "event_type": "invoke_end",
    "event_class": "lifecycle",
    "timestamp": 1767196805.0,
    "interaction_seq": 0,
    "session_id": "test-session",
    "conversation_id": "test-session",
    "agent_id": "test-agent",
    "trace_id": "test-trace",
    "context_id": "test-ctx",
    "llm_call_seq": -1,
    "tool_call_seq": -1,
    "subsession_id": "",
    "tool_call_id": ""
  },
  "payload": {
    "content": { "result": "文件列表: a.txt b.txt" }
  },
  "metadata": {}
}
```

### permission_interrupt_tool（安全检测事件）

```json
{
  "common": {
    "source": "AgentSSASSecurityRail",
    "event_type": "permission_interrupt_tool",
    "event_class": "security",
    "timestamp": 1767196806.0,
    "interaction_seq": 0,
    "session_id": "test-session",
    "conversation_id": "test-session",
    "agent_id": "test-agent",
    "trace_id": "test-trace",
    "context_id": "test-ctx",
    "llm_call_seq": -1,
    "tool_call_seq": 0,
    "subsession_id": "",
    "tool_call_id": "call-001"
  },
  "payload": {
    "tool_name": "bash",
    "tool_call_id": "call-001",
    "content": { "tool_args": { "command": "rm -rf /" } },
    "risk_source": "PermissionInterruptRail",
    "risk_type": "tool_permission_denied",
    "risk_level": "high",
    "decision": "reject",
    "evidence": { "reason": "工具未被授权调用" }
  },
  "metadata": {}
}
```

## 安全检测事件的 payload 字段说明

`permission_interrupt_tool` 是安全检测衍生事件（`event_class` 为 `security`），由 AgentSSASSecurityRail 观察到前序安全 Rail（如 PermissionInterruptRail）拦截了工具调用时产生。除工具事件共有的 `tool_name`、`tool_call_id`、`content` 外，payload 还包含以下安全检测字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `risk_source` | str | 风险来源 Rail 名（如 `PermissionInterruptRail`） |
| `risk_type` | str | 风险类型（如 `tool_permission_denied`） |
| `risk_level` | str | 风险等级，五级之一：safe / low / medium / high / critical |
| `decision` | str | 来源 Rail 已做出的安全决策（如 `reject`） |
| `evidence` | dict | 证据字典，说明判定原因 |

这些字段供 `security_rail_detection` 等订阅安全事件的检测模块消费，聚合结果最终体现为 `RiskAssessment`（含 `has_risk`、`risk_level`、`risk_type`、`risk_score`、`confidence`、`detected_threats`、`recommended_actions`、`details`、`evidence` 九个字段）。

## 相关文档

- [配置参考](./configuration.md)
- [检测模块参考](./detection-modules.md)
- [查看威胁日志](../how-to/view-threat-logs.md)
