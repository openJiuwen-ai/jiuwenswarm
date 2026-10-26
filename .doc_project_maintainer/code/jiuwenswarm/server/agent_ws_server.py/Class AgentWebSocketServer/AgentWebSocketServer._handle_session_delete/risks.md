---
symbol: AgentWebSocketServer._handle_session_delete
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_delete audit evidence

## ISSUE-001: Unvalidated session_id reaches recursive deletion.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Lines 2594-2606 only strip params.session_id before computing get_agent_sessions_dir() / target. Absolute paths can replace the root and '..' segments can escape it; no safe-name validation or resolved containment check precedes exists/is_dir and shutil.rmtree at line 2657.
- Suggested action: Require a safe single-name ID, enforce resolved-root containment, and test traversal and absolute paths.

## ISSUE-002: Delete leaves some session-scoped runtime uncoordinated.

- Dimension: `state_mutation`
- Severity: `medium`
- Status: `open`
- Evidence: The handler does not cancel/await _session_stream_tasks or remove _session_mode_sync_locks. For non-team metadata it calls Runner.release directly at line 2638 rather than AgentManager.cleanup_session_runtime, so existing adapters may retain per-session state while the directory is removed.
- Suggested action: Define idempotent ordering for active work, adapter cleanup, locks, and caches under concurrent chat traffic.

## ISSUE-003: Filesystem failure can leave a partial delete with a generic error.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: Runtime/team cleanup is attempted first at lines 2630-2646, but shutil.rmtree at line 2657 is outside that DELETE_FAILED mapping. A filesystem exception reaches the generic dispatcher after checkpoint/team state may already be released, while caches are cleared only after rmtree succeeds.
- Suggested action: Map filesystem errors locally and make partial cleanup observable and retry-safe.

## ISSUE-004: Direct tests cover only ordinary success and the checkpointer gate.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Two direct tests in test_agentserver_acp.py cover ordinary success and checkpointer rejection. Team behavior is adjacent TeamManager/team.delete coverage; missing/non-directory target, traversal/absolute target, concurrent stream, cleanup failure, rmtree failure, and cache ordering are untested.
- Suggested action: Add handler tests for those branches and failure modes.

## ISSUE-005: Recursive filesystem deletion blocks the AgentServer event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: shutil.rmtree(session_dir) runs synchronously inside this async WebSocket handler. A large or slow session tree can stall unrelated request tasks sharing the event loop.
- Suggested action: Run bounded recursive deletion in a worker thread after containment validation, and expose progress/timeout behavior if session trees can be large.
