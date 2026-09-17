---
symbol: AgentWebSocketServer._handle_browser_runtime_restart
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_browser_runtime_restart audit evidence

## ISSUE-001: A browser restart can block the AgentServer event loop for tens of seconds.

- Dimension: `performance_risk`
- Severity: `high`
- Status: `open`
- Evidence: Current async handler calls synchronous restart_local_browser_runtime_server directly. Its lifecycle can wait for terminate/kill, poll port release, and synchronously start the replacement, blocking the AgentServer loop and unrelated WebSocket tasks.
- Suggested action: Use asyncio.to_thread or an async dependency with a bounded timeout and cancellation-safe result handling.

## ISSUE-002: Concurrent restart requests race on process-global browser state.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: _handle_message dispatches requests concurrently, while the restart dependency reads then clears/stops/replaces process-global runtime process/URL state without a handler-level lock or single-flight guard.
- Suggested action: Serialize restart with a process-scoped async single-flight lock and make the global transition atomic.

## ISSUE-003: The config-save caller treats a failed restart response as success.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: The handler returns ok=false on exception, but current app_gateway config-save code awaits client.send_request(restart_env) and discards the response before returning true. On success, result=None also does not distinguish unsupported transport from no owned server.
- Suggested action: Define explicit outcomes; make Gateway check ok and trigger its restart fallback on failure.

## ISSUE-004: The restart RPC and config-save integration have no regression coverage.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Repository search finds no test reference to _handle_browser_runtime_restart, BROWSER_RUNTIME_RESTART, browser.runtime_restart, or the config-save restart response contract.
- Suggested action: Cover restart/no-op/failure, event-loop responsiveness, concurrency, response encoding, and Gateway failure propagation.
