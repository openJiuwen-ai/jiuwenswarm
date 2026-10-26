---
symbol: AgentWebSocketServer._handle_permissions_config
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_permissions_config`

## Actual Role

Acts as the AgentServer adapter for all routed `permissions.*` reads and mutations. It synchronously dispatches validation plus YAML access, classifies three methods as read-only, and for every other successful method synchronously snapshots the new config before registering a process-global fire-and-forget agent reload. It then preserves the dispatcher response envelope, encodes it, and serializes one WebSocket send under `send_lock`; runtime convergence is deliberately outside the response contract.

## Key Signals

- Input: An `AgentRequest` already routed by membership in `get_permissions_config_req_methods()`; the dispatcher owns method-specific parameter validation.
- Output: Normally one metadata-preserving E2A response from the dispatcher. Post-mutation snapshot/task-setup failures instead escape to `_handle_message`, which creates a generic error response after the change may already be durable.
- Main side effects: Synchronously reads or rewrites config YAML, snapshots config after successful mutations, registers a reload task in `_background_permission_reload_tasks`, and writes one wire frame.
- Main risk: Persistence success and runtime application are not one observable outcome: post-commit setup can fail the request, while later reload failure is debug-only and leaves clients believing the new policy is active.
- Related tests: `tests/unit_tests/agentserver/test_agentserver_cli_commands.py::test_handle_permissions_config_does_not_block_on_slow_reload` covers only a mocked successful mutation and eventual reload call. Read-only/error, post-commit failure, authorization, cancellation, send, and shutdown paths are missing. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
