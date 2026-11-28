# AG-UI integration guide

[中文](../zh/AG-UI.md)

## 1. Connection model

Applications send requests to Gateway's `POST /api/ag-ui/run` and receive text and tool calls over SSE. Gateway translates AG-UI into [E2A](E2A-protocol.md) for JiuwenSwarm; applications do not manage AgentServer sessions directly.

Gateway associates a host session with the authenticated identity and `threadId`. Keep the thread ID for subsequent requests and use a new `runId` for each run.

## 2. Enable the endpoint

Deploy an agent-gateway version containing the AG-UI route and a JiuwenSwarm version supporting client tools and `execution.status`.

| Gateway environment variable | Purpose |
| --- | --- |
| `AG_UI_ENABLED=1` | Enable the endpoint; disabled by default |
| `AG_UI_ALLOWED_ORIGINS` | Comma-separated browser Origin allowlist |
| `AG_UI_PROJECT_ID` | Optional host project |
| `AG_UI_MODEL_NAME` | Optional model selection |
| `AG_UI_ROLE_INSTRUCTIONS` | Optional application role instructions. Session creation trims surrounding whitespace and allows up to 12,000 characters |

The endpoint reuses WebChannel authentication. Cross-origin browser requests must also pass Origin validation. Application state cannot override server-controlled identity or permissions.

## 3. Start a conversation

Send `Content-Type: application/json` and `Accept: text/event-stream`. A first request can look like this:

```json
{
  "threadId": "counter-thread",
  "runId": "run-1",
  "messages": [{"id": "user-1", "role": "user", "content": "Set the counter to 3"}],
  "tools": [{
    "name": "set_counter",
    "description": "Set the counter",
    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
  }],
  "state": {"counter": 0},
  "context": []
}
```

Subsequent requests preserve the IDs, roles and order of synchronized messages, then append either one text user message or a complete batch of tool results. Do not mix both in one run. Retain assistant messages and tool calls received from the stream; sending only the latest user message is insufficient.

`state` and `context` supply temporary application data to the model during execution and tool recovery, without becoming persistent conversation messages. `forwardedProps` becomes host metadata; it is not automatically injected into the model and cannot change identity, channel or permissions. State-only runs are unsupported.

## 4. Execute application tools

`tools` declares the application's available tools for this run. Host tools remain available; application tools cannot shadow them and are not copied to child agents. Declare tools on every run: omitting the field or sending an empty array removes this integration's previous declarations.

1. Receive `TOOL_CALL_START / ARGS / END` and retain each call's ID, name and complete arguments.
2. Execute the batch in the application and update its state.
3. Start a new `runId`, append all `role=tool` results to the message history, match each `toolCallId` to its call, and include the latest `state`.
4. Gateway converts the results into native host interrupt recovery input so the model can continue.

Return the entire pending batch together. Missing, duplicate or unknown results are rejected; a new user message cannot replace pending results. Tool results currently support text only. An outstanding call can still be resolved after its tool is removed from the next declaration.

## 5. Completion, errors and recovery

The stream includes `RUN_STARTED`, text events, application tool events, and `RUN_FINISHED` or `RUN_ERROR`. Waiting for application tools also ends the current run; submit their results in a new run to continue the conversation.

Gateway uses host `execution.status` facts to establish completion or tool waiting. Text completion and connection closure alone do not establish success. Validation failures before streaming return HTTP errors; failures within the stream produce `RUN_ERROR`.

Disconnects and timeouts trigger targeted cancellation of the current host execution. Persisted tool waiting points support subsequent recovery, but SSE reconnection and automatic replay of in-flight execution are unsupported. Reopening a thread checks host history; mismatches must be resolved before continuing.

When cancelling a run that is waiting for tools, the host confirms cancellation after committing the checkpoint and recording the cancelled state. A write failure leaves cancellation unconfirmed.

## 6. Current scope

The implementation supports AG-UI 1.0 text conversations and application tool execution in ordinary Work root sessions. Host tools, MCP, skills and synchronous child agents remain available according to host configuration.

Code, Team, Plan, Goal, background dispatch, host approval interfaces and AG-UI `resume` input are not exposed. Questions use ordinary text. Tools requiring new human authorization return a rejection result; existing permission rules still apply.

This guide reflects the current source in both repositories. It does not establish end-to-end validation with a real model, browser application and Gateway.
