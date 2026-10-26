---
symbol: AgentWebSocketServer._handle_session_rewind_full
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_rewind_full`

## Actual Role

Multiplexes three RPC behaviors selected by route booleans: ordinary rewind, rewind-and-restore files, and rewind compact. It superficially validates the session and turn, optionally restores files, truncates history/diff through a rewind helper or `up_to` compact helper, best-effort rebuilds active context/checkpointer state from the already-mutated history, optionally queues compact-from marker/summary records, and sends one metadata-preserving E2A response under `send_lock`. It is a non-transactional coordinator, not merely a history rewind.

## Key Signals

- Input: `params.session_id`, `turn_index`, compact `direction`/summary/count fields, and route-selected `restore_files`/`compact` flags.
- Output: One encoded `AgentResponse`; success merges rewind data with `rewind_context` and optional restore or summarized-message fields. Validation uses `BAD_REQUEST`, while internal failures expose a raw error without a stable code.
- Main side effects: Restores/deletes files, rewrites history/metadata/diffs, mutates active context and checkpointer state, queues compact history records, and writes the wire response.
- Main risk: Unchecked session paths and non-atomic mutations can leave files, history, context, and checkpointer inconsistent while reporting success.
- Related evidence: `tests/unit_tests/test_compact_partial.py` covers helper branches and confirms summary records participate in reconstruction; no direct handler or routed rewind tests were found. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
