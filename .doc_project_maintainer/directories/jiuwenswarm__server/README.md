---
path: jiuwenswarm/server
encoded: jiuwenswarm__server
modules:
  - agentserver-runtime
confidence: confirmed
last_updated: 2026-08-01
read_when: "Editing AgentServer entrypoints, WebSocket dispatch, runtime services, sandbox, hooks, or gateway push."
---

# `jiuwenswarm/server`

## Purpose

Contains the AgentServer process entrypoint, the WebSocket server used by Gateway, runtime services behind agent execution, sandbox integration, hooks, utility services, and server-push helpers.

## Important Files

- `app_agentserver.py`: standalone AgentServer CLI/process lifecycle.
- `agent_ws_server.py`: central Gateway WebSocket server and AgentServer request dispatch surface.
- `gateway_push/wire.py`: server-push E2A response wire encoder.
- `gateway_push/transport.py`: default in-process transport that forwards push messages through the AgentServer singleton.
- `runtime/agent_manager.py`: channel/session agent instance management and config reload boundary.
- `runtime/agent_warm_pool.py`: lazy READY-slot reconciliation and foreground-priority session preparation.
- `runtime/proactive_adapter.py`: proactive recommendation engine attachment.
- `runtime/session/*`: session history and metadata support.
- `sandbox/jiuwenbox_runner.py`: jiuwenbox process runner.

## Related Flows

- `gateway-agentserver-e2a-chat`: normal request/response and streaming chat.
- `agentserver-session-lifecycle`: session state, history, rewind, delete, fork.
- `agentserver-server-push`: out-of-band downstream events.
- `agentserver-command-mcp`: MCP config mutation, reload, and tool discovery.
- `agentserver-sandbox-runtime`: persisted sandbox policy, JiuwenBox, and runtime application.
- `agentserver-plan-mode-exit`: checkpoint mode restoration and exit push.
- `agentserver-schedule-auto-harness`: durable scheduled tasks and autonomous execution.
- `agentserver-history-stream`: paged history storage-to-frontend reconstruction.
- `session-prewarm-allocation`: AgentServer-owned IDs, foreground claims, and lazy background replenishment.

## Related Code Symbols

- `_run`, `main`
- `AgentWebSocketServer._handle_message`
- `AgentWebSocketServer._handle_stream`
- `AgentWebSocketServer._handle_unary`
- `AgentWebSocketServer._handle_cancel`
- `AgentWebSocketServer.send_push`

## Coverage

Partial at directory scope. `agent_ws_server.py` has 158 required symbols (1 class, 29 top-level functions, 128 methods); all 128 class methods have entry docs and remain `agent_audited`. The normalized-AST scan at `10afedf2` found 0 source-expired method audits. Current integrity verification trusts 59 records and flags 69 non-source-expired cards whose entry-document hashes changed. The frozen queue excludes 6 newly observed unaudited methods, and file/class/function coverage plus other `jiuwenswarm/server` classes remain incomplete.
