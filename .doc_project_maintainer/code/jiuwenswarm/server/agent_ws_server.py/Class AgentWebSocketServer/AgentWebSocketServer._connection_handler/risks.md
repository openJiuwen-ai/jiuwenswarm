---
symbol: AgentWebSocketServer._connection_handler
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._connection_handler audit evidence

## ISSUE-001: Any connection close clears global active-connection state and cancels global work.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: Current source assigns _current_ws/_current_send_lock before ack; every connection's finally unconditionally clears both globals, cancels AgentManager and team work, and clears _session_stream_tasks without checking that ws still owns the active slot.
- Suggested action: Guard cleanup by active-slot ownership, or deliberately reject replacement connections.

## ISSUE-002: Scheduler shutdown is tied to per-connection cleanup.

- Dimension: `state_mutation`
- Severity: `medium`
- Status: `open`
- Evidence: The current per-connection finally block calls _stop_scheduler even though the scheduler is server state and the inline comment says server shutdown.
- Suggested action: Move scheduler stop to server shutdown, or define it as connection-scoped and test that contract.

## ISSUE-003: connection.ack ordering can race with server push.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `open`
- Evidence: _current_ws is published before send_wire_payload(connection.ack), and ack does not use send_lock; send_push can therefore write to the same socket first although Gateway waits for ack before normal traffic.
- Suggested action: Send ack under the same lock before publishing _current_ws, or gate send_push until ack completes.

## ISSUE-004: Completed request-task exceptions are not retrieved.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: task.add_done_callback(tasks.discard) removes completed tasks without reading exceptions. _handle_message can raise before its dispatch try from _payload_to_request or malformed legacy fallback metadata, so finally will not gather that already-discarded failure.
- Suggested action: Retrieve and log task exceptions in the callback, or keep tasks until gather consumes them.

## ISSUE-005: Inbound frames create unbounded concurrent request tasks.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: Each frame immediately calls asyncio.create_task and adds it to a set; there is no per-connection task limit, queue, semaphore, or read-side backpressure tied to handler completion.
- Suggested action: Bound concurrent request tasks and pause or reject reads when the connection reaches that limit.
