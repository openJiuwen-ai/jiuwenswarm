# Event Format Reference

Raw events (raw_event) collected by AgentSSASSecurityRail use a three-layer structure: `common` (shared correlation fields) / `payload` (event content) / `metadata` (reserved extension layer). This document provides an overview of the three-layer structure, field-by-field descriptions of the 14 common-layer fields, a catalog of the 7 event types, and complete JSON examples.

Related documentation:

- [Detection Modules Reference](./detection-modules.md): how detection modules subscribe to these events
- [HTTP API Reference](./http-api.md): reporting raw_events over HTTP
- [View Threat Logs](../how-to/view-threat-logs.md): how to query events after they are persisted

## Three-Layer Structure Overview

```text
raw_event
├── common     # Shared correlation fields (14): identify the event source, type, and position in the session/interaction/call chain
├── payload    # Event content: carries inputs/outputs or security detection results by event type
└── metadata   # Reserved extension layer, currently an empty dict
```

| Layer | Responsibility | Notes |
|-------|----------------|-------|
| `common` | All identity fields needed for event routing and correlation | Structure is fixed for all event types |
| `payload` | The concrete content of the event | Lifecycle events contain a `content` key; tool events additionally contain `tool_name` and `tool_call_id`; security detection events contain `risk_*` fields |
| `metadata` | Reserved for extension | Currently always an empty object |

## common Layer Fields (14)

| Field | Type | Description |
|-------|------|-------------|
| `source` | str | Reporting source identifier, such as `AgentSSASSecurityRail` |
| `event_type` | str | Event type; see the 7 event types below |
| `event_class` | str | Event class: `lifecycle` (lifecycle events) / `security` (security detection derived events) |
| `timestamp` | float | Event timestamp (Unix seconds) |
| `interaction_seq` | int | Interaction sequence number; the complete process from one user request to the final response counts as one interaction; auto-incrementing int |
| `session_id` | str | Unique session identifier |
| `conversation_id` | str | Session display ID, usually identical to session_id |
| `agent_id` | str | Agent ID |
| `trace_id` | str | Trace ID, spanning one complete trace |
| `context_id` | str | Context ID |
| `llm_call_seq` | int | LLM call sequence number; for llm_call events, the number of the current LLM call; for tool_call events, the number of the LLM call it belongs to; -1 when not applicable |
| `tool_call_seq` | int | Auto-incrementing tool call sequence number; -1 for non-tool events |
| `subsession_id` | str | Subsession ID |
| `tool_call_id` | str | Tool call ID assigned by the reporter; used to explicitly correlate the same tool call across tool_input / tool_output events |

The convention for the sequence fields (`interaction_seq`, `llm_call_seq`, `tool_call_seq`): the default is `-1`, meaning the event does not carry this sequence number; the first valid value is `0`, incrementing thereafter.

On database storage of the time field: when the raw_event's `timestamp` (Unix floating-point seconds) is written to the database (the events / alerts / raw_events tables), a `timestamp_text` human-readable column (local-timezone `YYYY-MM-DD HH:MM:SS.mmm` format) is written alongside it, convenient for direct inspection; the `timestamp` column remains Unix floating-point seconds, used for computation and sorting. When an older database is opened, the column is added automatically by migration and historical rows are backfilled.

## Event Catalog (7 Types)

| Event type | event_class | Key payload fields | Description |
|------------|-------------|--------------------|-------------|
| `invoke_start` | lifecycle | `content.query` | Interaction start, user input |
| `llm_input` | lifecycle | `content.messages` | LLM call input (prompt) |
| `tool_input` | lifecycle | `tool_name`, `tool_call_id`, `content.tool_args` | Tool call input (arguments) |
| `tool_output` | lifecycle | `tool_name`, `tool_call_id`, `content.tool_result` | Tool call output (result) |
| `llm_output` | lifecycle | `content.response` | LLM call output (response) |
| `invoke_end` | lifecycle | `content.result` | Interaction end, final result |
| `permission_interrupt_tool` | security | `risk_source`, `risk_type`, `risk_level`, `decision`, `evidence` (plus `tool_name`, `tool_call_id`, `content`) | Security detection derived event: another security rail intercepted a dangerous tool call |

The event stream of one complete interaction runs:

```text
invoke_start -> llm_input -> tool_input -> tool_output -> llm_output -> invoke_end
```

(In interactions without tool calls, `tool_input` / `tool_output` do not appear; with multiple tool calls, the corresponding events can appear in multiple pairs.)

## Complete Examples per Event Type

The following examples match the structure produced by `tests/fixtures/event_factory.py` (a synthetic raw_event generator). The `metadata` layer is always an empty object at present; the only differences omitted from these examples are the values themselves.

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

### permission_interrupt_tool (Security Detection Event)

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

## Payload Fields of Security Detection Events

`permission_interrupt_tool` is a security detection derived event (`event_class` is `security`), produced when AgentSSASSecurityRail observes that a preceding security rail (such as PermissionInterruptRail) has intercepted a tool call. Besides the `tool_name`, `tool_call_id`, and `content` shared with tool events, the payload also carries the following security detection fields:

| Field | Type | Description |
|-------|------|-------------|
| `risk_source` | str | Name of the rail that raised the risk (such as `PermissionInterruptRail`) |
| `risk_type` | str | Risk type (such as `tool_permission_denied`) |
| `risk_level` | str | Risk level, one of five: safe / low / medium / high / critical |
| `decision` | str | Security decision already made by the source rail (such as `reject`) |
| `evidence` | dict | Evidence dictionary explaining the verdict |

These fields are consumed by detection modules that subscribe to security events, such as `security_rail_detection`; the aggregated result ultimately surfaces as a `RiskAssessment` (with the nine fields `has_risk`, `risk_level`, `risk_type`, `risk_score`, `confidence`, `detected_threats`, `recommended_actions`, `details`, and `evidence`).

## Related Documentation

- [Configuration Reference](./configuration.md)
- [Detection Modules Reference](./detection-modules.md)
- [View Threat Logs](../how-to/view-threat-logs.md)
