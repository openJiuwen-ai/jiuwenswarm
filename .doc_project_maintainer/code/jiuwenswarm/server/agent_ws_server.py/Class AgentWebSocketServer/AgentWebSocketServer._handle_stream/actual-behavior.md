---
symbol: AgentWebSocketServer._handle_stream
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_stream`

## Actual Role

Runs one AgentServer stream. It records the current task by session, chooses a lightweight stateless agent or resolves the mode/project-aware agent, synchronizes code plan state, and starts an idle heartbeat. Real chunks receive request `agent_ref`, zero-based E2A sequence numbers, size-bounded sending, and shared-lock serialization; keepalives use sequence `-1`. It stops after a closed socket or oversized-chunk fallback, then cancels heartbeat state, conditionally removes its task entry, and checks post-stream plan-mode exit.

## Key Signals

- Input: WebSocket, `AgentRequest`, and the connection's shared send lock; missing channel/session fall back to `default`, while method/mode/project validation is delegated.
- Output: returns `None`; sends E2A wire chunks with zero-based sequence numbers and idle keepalives with sequence `-1`.
- Side effects: mutates `_session_stream_tasks`, starts/cancels heartbeat, may persist code-mode state, invokes the agent stream, writes bounded frames, and may push `plan.mode_exited`.
- Main risks: task registration precedes cleanup; a single session slot loses concurrent-stream ownership; unexpected heartbeat failure can escape finalization.
- Tests: two mode tests cover team/code-team selection and one encoded chunk. `test_stream_stops_after_oversized_chunk_is_replaced` covers early stop after bounded-send fallback. No direct heartbeat, same-session concurrency, cancellation, early lookup failure, stateless, plan-exit, or unexpected-send-failure test was found. Tests were not run in this review.

## Detail Index

- Detail docs pending.
