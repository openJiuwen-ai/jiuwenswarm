---
symbol: AgentWebSocketServer._handle_command_model
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_model`

## Actual Role

Handles direct unary `command.model` requests through three divergent branches: `add_model` acknowledges only the supplied name without persistence; `switch_model` rejects missing env updates or placeholder API bases, otherwise writes arbitrary process environment values, best-effort clears config cache and reloads agents, then reports applied success; any other action returns current `MODEL_NAME` with a hard-coded available list. It converts caught exceptions to one error response and sends under `send_lock`.

## Key Signals

- Input: `params.action` plus `target`, `model`, and `env_updates` for supported actions.
- Output: One response; stub add/status can report success without durable configuration, and switch reports `applied: true` despite softened cache/reload failures.
- Main side effects: Mutates process-global environment, clears global config cache, reloads shared agents, logs update values, and sends a frame.
- Main risk: Direct behavior diverges from Gateway management, has non-transactional concurrent global mutation, can report partial application, and can disclose credentials.
- Related tests: Direct tests cover no-action status and add_model only; Gateway tests exercise a separate switch implementation. Direct switch validation/failure/concurrency/log-redaction paths are missing; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
