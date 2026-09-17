---
symbol: AgentWebSocketServer._handle_browser_start
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_browser_start audit evidence

## ISSUE-001: Process creation is reported as browser readiness.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, the handler treats any normal return from synchronous start_browser as ok=true and forwards only returncode. The launcher returns after Popen/profile persistence without bounded Chrome/CDP readiness or immediate-exit detection, while the BrowserPanel interprets the RPC as started.
- Suggested action: Probe bounded CDP readiness; return pid/endpoint/status and terminate failed children.

## ISSUE-002: Browser start is neither idempotent nor lifecycle-managed.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, every browser.start request invokes start_browser(dry_run=False) from global config. The launcher has no handler-visible ensure-running lock, existing-instance/ownership check, rate limit, retained process handle, or paired cleanup path for the same port/profile.
- Suggested action: Use a singleton runtime manager with serialized ensure-running and explicit stop/ownership.

## ISSUE-003: Post-spawn failure can return an error while leaving an orphan browser.

- Dimension: `error_handling`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, the downstream child is detached before profile-store persistence. If that post-spawn write raises, this broad except sends ok=false with only the exception string and has no child handle with which to terminate or report the already-created process.
- Suggested action: Retain the child handle for rollback and expose partial-state diagnostics.

## ISSUE-004: The RPC can expose an unauthenticated CDP endpoint beyond loopback.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, this handler passes the global config path without validating launch policy. start_browser accepts config/environment remote_debugging_address and forwards it to Chrome without a loopback guard, so a broad bind can expose the CDP control surface.
- Suggested action: Enforce loopback unless separately authorized for remote CDP exposure.

## ISSUE-005: No handler, launcher, or end-to-end browser-start tests were found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, no direct handler, launcher, or Gateway-to-AgentServer browser-start test was found. Early exit/readiness, duplicates, profile-write failure, remote bind, event-loop blocking, nonzero returncode, wire errors, and BrowserPanel outcomes are unverified.
- Suggested action: Add fake-process tests and a Gateway-to-AgentServer lifecycle integration test.

## ISSUE-006: Synchronous launch work blocks the AgentServer event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, the async handler directly calls synchronous start_browser. Config loading, executable/profile resolution, process creation, and profile persistence complete on the WebSocket event-loop thread before the response can be encoded or other work can resume.
- Suggested action: Run the blocking launcher in a worker thread or replace it with an async lifecycle service with a bounded readiness deadline.
