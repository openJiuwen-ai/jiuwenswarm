# AgentServer Runtime Design

## Runtime Boundary

AgentServer is a separate process from Gateway in split deployment. `app_agentserver._run` creates extension infrastructure, starts `AgentWebSocketServer`, initializes proactive recommendations, and runs a remote teammate bootstrap daemon until shutdown.

`AgentWebSocketServer` accepts Gateway WebSocket connections. A successful connection receives a `connection.ack` event. Each incoming frame is handled concurrently by `_handle_message` and serialized outbound writes use a per-connection `send_lock`.

## Dispatch Strategy

`_handle_message` first parses JSON. It prefers `E2AEnvelope.from_dict` and falls back to legacy request parsing when conversion fails or the E2A envelope carries fallback metadata. It injects ACP client capabilities for ACP requests, triggers before-chat hooks for chat-like requests, then routes explicit `ReqMethod` values to specialized handlers before falling through to `_handle_stream` or `_handle_unary`.

## Stateful Guards

- `_session_stream_tasks` maps session IDs to active stream tasks and stop events for cancellation.
- `_acp_client_capabilities_by_ws` records capabilities per WebSocket identity.
- `_session_mode_sync_locks` serializes code-mode restore per session.
- `_plan_exited_sessions` prevents plan-mode race re-entry after `exit_plan_mode`.
- `_scheduler_service` is lazily initialized for schedule requests.
- `_jiuwenbox_runner` owns optional internal sandbox subprocess lifecycle.

## Risk Areas

The file is intentionally broad and central. Follow-up slices should separate dispatch, session/history, command/config mutation, sandbox, scheduler, and server-push symbol docs.
