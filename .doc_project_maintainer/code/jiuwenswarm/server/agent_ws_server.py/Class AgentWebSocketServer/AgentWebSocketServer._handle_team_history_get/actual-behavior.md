---
symbol: AgentWebSocketServer._handle_team_history_get
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_team_history_get`

## Actual Role

Handles unary `team.history.get` by requiring a nonblank string session id, optionally selecting member-visible team records, and offloading the complete history read/filter to a worker thread. It sanitizes the full result, clamps cursor/limit/byte-budget values, selects a byte-bounded page, logs pagination data, and sends one E2A response under `send_lock`; reader exceptions are softened into successful empty history.

## Key Signals

- Input: `params.session_id`; optional nonblank `member_name`; `cursor` or legacy `offset`; optional `limit` and `max_bytes`, all coerced/clamped by shared helpers.
- Output: One AgentResponse with `records`, `session_id`, `cursor`, `next_cursor`, `has_more`, and `total`, or `ok: false` when session id is missing.
- Main side effects: Runs filesystem reads/retries in a thread, may create a session directory through read-path helpers, logs read/paging state, and sends a WebSocket frame.
- Main risk: Unchecked session paths and false-empty read failures combine with full-history work per page.
- Related tests: `test_history_payload_limits.py` covers paging, cursor continuation, response byte bounds, truncation, and oversized placeholders. Missing-id, reader-failure, member-scope, coercion, path-side-effect, and traversal cases were not found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
