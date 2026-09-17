---
symbol: AgentWebSocketServer._handle_command_workflows
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_workflows`

## Actual Role

Serves `command.workflows` as a live-first, checkpoint-fallback read. It defaults missing channel identity to `web`, lazily accesses the process-wide TeamManager, and uses the request session to select a live workflow handler. With no handler it synchronously restores persisted runs and serializes each run; with a handler it reads the in-memory snapshot and logs names/statuses. Both branches normalize through `_build_workflow_snapshot_payload`, degrade backend/serialization failures to a successful empty snapshot, and send exactly one frame under `send_lock`.

## Key Signals

- Input: Request-level `session_id`, `channel_id`, and request ID after `COMMAND_WORKFLOWS` routing; params are ignored and blank channel defaults to `web`.
- Output: An `ok: true` `AgentResponse` shaped as `{type, workflows, session_id, total, truncated}`. Real emptiness, restore failure, and live snapshot failure share that same success contract; request metadata is not copied.
- Main side effects: May initialize the global TeamManager, synchronously reads checkpoint metadata when no live handler exists, emits detailed workflow logs, and sends one WebSocket frame.
- Main risk: An unvalidated session ID reaches storage, and failure-as-empty semantics can erase diagnostic distinction; live read failure also skips the otherwise available persisted fallback.
- Related tests: Nine direct tests cover live/empty snapshots, size limits, defaults, and live-handler failure. Restore success/failure, unsafe IDs, send failure, and `_handle_message` dispatch remain untested. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
