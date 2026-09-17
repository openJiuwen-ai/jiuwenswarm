---
symbol: AgentWebSocketServer._handle_command_workflows
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_workflows audit evidence

## ISSUE-001: session_id is not validated before checkpoint metadata restore.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, request.session_id is reduced only to request.session_id or an empty string, then passed to restore_workflow_runs when no live handler exists. The downstream metadata restore composes the session directory without this handler enforcing a canonical ID or resolved-path containment.
- Suggested action: Validate session_id as a safe session identifier before fallback restore, or use a metadata accessor that enforces containment.

## ISSUE-002: Snapshot and restore failures are reported as successful empty snapshots.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, checkpoint restore exceptions and live get_workflow_snapshot/serialization exceptions are logged, then returned as ok=true empty workflow_run_snapshot payloads. If a live handler exists but fails, the persisted checkpoint fallback is not attempted.
- Suggested action: Use the persisted snapshot after live-read failure when safe, and otherwise return diagnostic metadata or ok=false while preserving empty success for real no-data cases.

## ISSUE-003: Fallback and dispatch behavior are not fully tested.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Nine direct tests cover empty/live snapshots, size bounds, live-handler failure, and defaults; none inject restored runs, restore failure, unsafe session IDs, send failure, or execute _handle_message routing.
- Suggested action: Add tests for dispatcher routing, checkpoint restore success, restore exception, and unsafe or invalid session_id handling.

## ISSUE-004: Checkpoint fallback performs synchronous restore work in the async request path.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, restore_workflow_runs(session_id) is called synchronously inside the async WebSocket handler before response encoding and send; metadata I/O and run deserialization therefore occupy the event-loop request path.
- Suggested action: Move checkpoint loading to an async storage boundary or a worker thread, with a bounded timeout.
