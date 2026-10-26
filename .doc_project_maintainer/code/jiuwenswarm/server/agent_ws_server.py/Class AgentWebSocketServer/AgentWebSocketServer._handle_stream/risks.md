---
symbol: AgentWebSocketServer._handle_stream
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_stream audit evidence

## ISSUE-001: Stream task registration can leak stale session entries before cleanup starts.

- Dimension: `state_mutation`
- Severity: `medium`
- Status: `open`
- Evidence: Current HEAD 39feee89 registers current_task in _session_stream_tasks at lines 2195-2197. Stateless/regular agent lookup and code-mode synchronization at lines 2202-2211 still run before the only cleanup try/finally begins around line 2264, so any early exception leaves the entry behind.
- Suggested action: Wrap registration, agent resolution, heartbeat setup, streaming, and cleanup in one outer try/finally.

## ISSUE-002: Heartbeat loop no longer leaves pending wait tasks behind.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `fixed`
- Evidence: Current lines 2224-2227 directly await heartbeat_event.wait() through asyncio.wait_for; the prior ensure_future/asyncio.wait pattern that could abandon a pending wait task is absent.
- Suggested action: No code change required; add a heartbeat lifecycle regression test when practical.

## ISSUE-003: Unexpected heartbeat failures can surface late and mask the stream outcome.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: The heartbeat loop catches only cancellation and WebSocketConnectionClosed. Finalization cancels and awaits it but handles only those same exceptions; an already-failed heartbeat send (for example OSError or fallback RuntimeError) can therefore replace a successful stream result or mask the primary stream exception.
- Suggested action: Capture and log unexpected heartbeat exceptions, and prevent heartbeat teardown from replacing the primary stream exception or successful result.

## ISSUE-004: One task slot per session cannot represent concurrent streams.

- Dimension: `state_mutation`
- Severity: `medium`
- Status: `open`
- Evidence: _session_stream_tasks[session_id] = current_task overwrites any existing stream for that session. Cleanup's identity guard protects the newer entry, but CHAT_CANCEL reads and cancels only the current single slot; an older concurrent stream remains active and untracked. The connection handler can create concurrent request tasks, and no same-session rejection is enforced here.
- Suggested action: Use request-keyed or per-session task sets, or atomically cancel/reject the predecessor before replacement; add a same-session concurrency and cancel test.
