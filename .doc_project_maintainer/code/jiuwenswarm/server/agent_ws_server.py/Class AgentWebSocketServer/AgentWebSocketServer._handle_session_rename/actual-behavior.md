---
symbol: AgentWebSocketServer._handle_session_rename
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_rename`

## Actual Role

Handles unary `session.rename` by choosing the connection session id and a default `tui` initialization channel, then passing the original params to shared `apply_session_rename`. The helper implements query (`title` absent), clear (blank title), and set (trimmed, 200-character title), including metadata creation for mutations; this method converts its tuple into one E2A success/failure response and sends it under `send_lock`, while unexpected exceptions fall through to `_handle_message`.

## Key Signals

- Input: `AgentRequest.params` may contain `session_id` and/or `title`; `request.session_id` is the fallback session id; blank `channel_id` becomes helper `init_channel_id="tui"`.
- Output: Success payload contains effective `session_id`, current `title`, and `previous_title`; helper-declared failure contains `error`/`code`, while unexpected failure is normalized by `_handle_message`.
- Main side effects: Set/clear may synchronously create missing metadata, then update the cache and enqueue the title write; query avoids directory creation. The method sends one bounded WebSocket frame.
- Main risk: The effective session id reaches filesystem path construction without containment validation, and success reflects the cache before asynchronous persistence completes.
- Related tests: No direct rename method/helper test was found. `test_session_metadata.py` verifies adjacent init/update/get/cache/queue behavior but not traversal, helper semantics, durability failure, response encoding, or WebSocket send failure.

## Detail Index

- Detail docs pending.
