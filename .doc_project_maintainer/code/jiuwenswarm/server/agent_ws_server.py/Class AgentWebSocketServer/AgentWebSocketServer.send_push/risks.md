---
symbol: AgentWebSocketServer.send_push
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer.send_push audit evidence

## ISSUE-001: Global connection replacement can misroute or disable pushes.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Lines 6092-6101 check process-global _current_ws/_current_send_lock, acquire whichever lock is current at context-manager entry, then re-read _current_ws only after the await. A reconnect can therefore pair the old socket's lock with the new socket; independently, the old _connection_handler finally unconditionally clears both globals and can erase the newer connection.
- Suggested action: Publish and snapshot an immutable connection object containing socket, lock, readiness, and generation; send and clear only while that generation still owns the slot.

## ISSUE-002: A push can violate the Gateway's mandatory first-frame ack contract.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: _connection_handler publishes the socket/lock before sending connection.ack, and the ack send itself does not use that lock. Gateway connect consumes exactly one first frame and marks server_ready only if it is connection.ack; a racing push can arrive first, be discarded as a non-ack frame, and leave readiness false. Ack failure is only logged while the socket remains published.
- Suggested action: Serialize and successfully send ack before publishing a ready connection; close/discard the generation when ack fails.

## ISSUE-003: Delivery failure is indistinguishable from success to callers.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 6092-6096 drop when disconnected, lines 6102-6107 turn an oversized original into a pushed error frame, and lines 6120-6121 swallow encode/send failures; all paths return None. Cron reports 'forwarded' after awaiting this method, while the proactive notification callback returns true whenever no exception escapes, so their callers can claim success after a drop.
- Suggested action: Return or raise a structured delivery result (sent, degraded, disconnected, failed) and define retry/queueing semantics for durable or interactive events.

## ISSUE-004: An unbounded send holds the lock shared with request responses.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: send_wire_payload enforces a byte budget, but its ws.send has no timeout and runs under the same per-connection lock used by request response handlers. Network backpressure can therefore hold the lock indefinitely and block every response and later push on that connection.
- Suggested action: Bound send latency, close or quarantine a stalled connection generation, and consider a bounded prioritized outbound queue so pushes cannot starve RPC responses.

## ISSUE-005: The transport edge and wire branches lack direct tests.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: The transport test only verifies delegation to the singleton. send_wire_payload tests cover byte accounting, fallback frames, and preservation of the server-push marker, but no direct AgentWebSocketServer.send_push test covers normal send, disconnect, reconnect generation, ack ordering/failure, timeout, or result semantics; the server-push flow still records response_kind and metadata/session branch tables as pending.
- Suggested action: Add direct connection-generation, ack-race/failure, backpressure/result tests and table-driven build_server_push_wire/Gateway inverse tests for every supported branch.

## ISSUE-006: The implicit push schema permits wire-success followed by downstream parse or routing loss.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: send_push validates neither msg type nor required routing/body fields. build_server_push_wire coerces missing request/channel IDs to empty strings and treats any truthy response_kind as a structured E2A response; the Gateway push inverse supports only specific kinds and raises on unsupported ones, so the server can log 'wire sent' for a frame the Gateway subsequently drops. Malformed body/metadata values instead fail encoding and are swallowed.
- Suggested action: Define typed chunk and structured-push variants, validate required identity/routing fields and an explicit supported response_kind allowlist before sending, and return validation errors to producers.
