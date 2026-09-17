---
symbol: AgentWebSocketServer._handle_command_add_dir
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_add_dir`

## Actual Role

Handles unary `command.add_dir` by extracting `params.path`, rejecting only missing or blank string values, stringifying other values, and synchronously asking the permissions helper to resolve and persist the path as a global `external_directory: allow` rule. It returns the original path, an echoed but behaviorally inert `remember`, and the helper result under `send_lock`; helper exceptions become an error payload. It intentionally performs no agent reload.

## Key Signals

- Input: `params.path`; optional `remember` is echoed but does not change behavior.
- Output: One AgentResponse whose `ok` mirrors nested `persist.ok`; exception responses contain top-level error text without a stable code.
- Main side effects: Synchronously read-modify-writes global config permissions on the event loop, logs exceptions, and sends a WebSocket frame; active agents are not reloaded.
- Main risk: A routed command can durably trust an arbitrary resolved path with only non-empty validation.
- Related tests: Two direct tests cover success payload and intentional no-reload behavior. Real YAML persistence, invalid/non-dict input, helper/send failure, and authorization boundaries are missing; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
