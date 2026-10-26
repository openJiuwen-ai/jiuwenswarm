---
symbol: AgentWebSocketServer._handle_team_snapshot
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_team_snapshot`

## Actual Role

Serves a live-only `team.snapshot` projection. It defaults missing channel identity to `web`, uses the request-level session ID to look up a monitor in the process-wide TeamManager, refuses to query absent or stopped handlers, and otherwise awaits the monitor's combined member/task/team projection. It has no persisted-state fallback: every unavailable, stopped, falsy, or failed snapshot is normalized to the same successful empty payload before one send under `send_lock`.

## Key Signals

- Input: Request-level `session_id` and `channel_id`; params are ignored, blank session looks up `""`, and blank channel defaults to `"web"`.
- Output: One E2A `AgentResponse` with `members`, `tasks`, and `team_id`; all normal degradation paths remain `ok: true`, and request metadata is not copied into the response.
- Main side effects: May lazily construct the global TeamManager, reads the live monitor, logs snapshot failures, and sends one wire frame.
- Main risk: The successful-empty normalization can make a transient task/member query failure indistinguishable from an intentionally empty or stopped team and can cause refresh consumers to discard valid visible state.
- Related tests: Monitor payload-shaping and team-state broadcast-helper tests exist, but direct handler and dispatch-route coverage was not found. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
