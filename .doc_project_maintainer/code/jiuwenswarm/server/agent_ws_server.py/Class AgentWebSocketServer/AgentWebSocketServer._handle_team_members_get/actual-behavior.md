---
symbol: AgentWebSocketServer._handle_team_members_get
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_team_members_get`

## Actual Role

Implements the internal `team.members.get` RPC used by Gateway `/join` seat validation. It prefers `params.session_id`, normalizes the requested team name only with `str(...).strip()`, and delegates to a helper that constructs a session-suffixed team DB key, lazily initializes shared SQLite state, reads persisted members without requiring a live monitor, and filters `role == "human_agent"`. The handler forwards the helper's members and nominally resolved team name in an always-success response, degrading unexpected helper failures to empty/None, then sends one locked frame.

## Key Signals

- Input: Request channel, requested `team_name`, and session ID with `params.session_id` taking precedence over `request.session_id`; session identity is not validated locally.
- Output: One `ok: true` unary response with `members` and `team_name`. The latter is currently the requested name echoed through the helper, not an independently resolved identity; request metadata is not copied.
- Side effects: May initialize the shared team database and its global tables/connections, reads persisted team-member rows, logs failures, and sends one WebSocket frame.
- Main risk: `/join` relies on this result for identity and seat validation, but caller-derived naming, successful-empty error folding, and an echoed team name can make the intended session/team consistency check non-authoritative.
- Tests/flow: Handler tests cover mocked payload passthrough and gateway tests cover the comparison value object separately; the real storage-backed helper and end-to-end `/join` decision are not exercised. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
