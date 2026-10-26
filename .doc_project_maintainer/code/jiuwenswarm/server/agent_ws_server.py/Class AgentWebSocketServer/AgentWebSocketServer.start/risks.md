---
symbol: AgentWebSocketServer.start
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer.start audit evidence

## ISSUE-001: Best-effort sandbox bootstrap can delay post-start initialization.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: Current HEAD 39feee89 still binds and logs the listener before awaiting _bootstrap_internal_jiuwenbox(). For explicit internal startup on Linux, that callee invokes JiuwenBoxRunner.ensure_running without overriding its 30-second default timeout; app_agentserver initializes ProactiveEngine and logs ready only after start() returns.
- Suggested action: Run jiuwenbox bootstrap in a named background task, or bound it with a shorter startup-specific timeout if server.start() should return promptly.

## ISSUE-002: Missing direct lifecycle tests for start().

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: tests/unit_tests/test_app_agentserver.py replaces get_instance() with a fake whose start() only appends an event. No located test directly invokes AgentWebSocketServer.start with serve, checkpointer, duplicate-start, ImportError fallback, bootstrap, bind-failure, or cancellation paths.
- Suggested action: Add async tests for normal start, duplicate-start no-op, legacy/fallback serve behavior, checkpointer failure propagation, and bootstrap call ordering.

## ISSUE-003: Cancellation during post-bind sandbox bootstrap can leave the listener open.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: Current HEAD assigns self._server before awaiting _bootstrap_internal_jiuwenbox(); that helper catches Exception, while asyncio.CancelledError remains uncaught. app_agentserver establishes its shutdown try/finally only after start(), ProactiveEngine initialization, and daemon setup, so cancellation during bootstrap can propagate with the listener still bound and no server.stop() call.
- Suggested action: Wrap post-bind initialization in cancellation cleanup that closes the bound server before re-raising, or move optional bootstrap outside the listener-start contract.
