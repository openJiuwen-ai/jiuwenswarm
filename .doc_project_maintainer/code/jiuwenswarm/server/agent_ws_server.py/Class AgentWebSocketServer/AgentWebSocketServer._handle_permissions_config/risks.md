---
symbol: AgentWebSocketServer._handle_permissions_config
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_permissions_config audit evidence

## ISSUE-001: Direct coverage is limited to the nonblocking mutation-reload path.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, test_handle_permissions_config_does_not_block_on_slow_reload covers only one mocked successful tools.update and waits for its slow reload. Read-only, dispatcher-error, get_config failure after mutation, reload failure/cancellation, send failure, and task lifecycle paths remain untested.
- Suggested action: Add async tests for read-only no-reload, mutation reload, dispatcher error no-reload, and reload-exception behavior.

## ISSUE-002: Mutation success is returned before the background reload outcome is known.

- Dimension: `observability`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, a successful mutation schedules reload_agents_config and immediately sends the dispatcher success. _log_permission_reload_failure reports a later exception only at debug, so the client cannot distinguish persisted-and-applied from persisted-but-runtime-stale state.
- Suggested action: Document eventual consistency, log at warning, or include response metadata when runtime permissions may be stale.

## ISSUE-003: No local authorization or channel gate protects permissions mutations.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, _handle_message routes solely by req_method membership and this handler delegates sensitive reads and mutations without an AgentServer-side admin, channel, or session gate; only the broader WebSocket origin boundary is visible locally.
- Suggested action: Document trusted-Gateway assumptions or add an explicit admin/channel/session gate.

## ISSUE-004: A post-mutation snapshot failure turns a committed change into an error response.

- Dimension: `error_handling`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, dispatch_permissions_config_request can persist a mutation before get_config() is evaluated for create_task. That synchronous read and task setup are outside a local try/except; on failure, _handle_message emits a generic error even though the YAML mutation may already be committed and no reload was scheduled.
- Suggested action: Capture the reload snapshot before committing, or isolate post-commit scheduling failures and return an explicit partial-success/reload-pending result.

## ISSUE-005: The async WebSocket handler performs synchronous config I/O on the event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, dispatch_permissions_config_request synchronously reads or rewrites YAML, and successful mutations synchronously call get_config() before create_task. The background task removes only the agent reload itself from the response critical path.
- Suggested action: Move config persistence and snapshot reads to an async service or worker thread, preserving serialized mutation semantics.
