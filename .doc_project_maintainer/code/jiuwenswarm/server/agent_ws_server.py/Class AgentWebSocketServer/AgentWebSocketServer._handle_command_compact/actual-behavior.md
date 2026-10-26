---
symbol: AgentWebSocketServer._handle_command_compact
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_compact`

## Actual Role

Coordinates manual context compaction across runtime state, durable transcript records, and UI notifications. It defaults session/channel and mode, resolves a project-scoped agent, invokes `compress_context(..., return_state=True)`, extracts result/stats/state/summary, and for a compressed result with truthy stats sends aggregate metrics. With a non-empty summary it then appends a compact boundary plus transcript-only summary and sends a richer compression-state event. Finally it returns result/stats and duplicate `summary`/`compact_summary` aliases; any exception after runtime compression is still converted to a generic failed RPC.

## Key Signals

- Input: Request session/channel, project identity, and optional `params.mode`; missing session/channel default to `default`, params is assumed dict-like, and `params.instructions` is ignored.
- Output: One locked response containing result, stats, and optional duplicate summary fields, or a raw-error failure. Push delivery and history durability are not represented in the success payload.
- Main side effects: Mutates runtime context/checkpoint state, sends up to two best-effort pushes, and separately queues compact boundary/summary history records plus metadata updates.
- Main risk: The operation is non-atomic: a durable/UI side effect can fail after compression, causing an error response for an already-mutated session and encouraging unsafe retry; missing session identity can target `default`.
- Related tests: Two direct tests cover a compressed response and detailed push. Routing, defaults/invalid input, no-op/busy, persistence and push failures, and post-commit error semantics are missing. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
