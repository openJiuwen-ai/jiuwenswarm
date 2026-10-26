---
symbol: AgentWebSocketServer._handle_schedule_request
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_schedule_request audit evidence

## ISSUE-001: Scheduled work can execute through another request's agent and workspace.

- Dimension: `boundary_safety`
- Severity: `critical`
- Status: `open`
- Evidence: At HEAD 39feee89 and in agentserver-schedule-auto-harness, one server-wide AutoHarnessService stores one mutable _agent. create/run/cancel/delete/issue_watch_once overwrite it from the current channel/mode/project request, while persisted tasks omit channel, project, mode, and agent cache identity; Scheduler later uses that mutable slot and hard-codes execution channel_id=tui.
- Suggested action: Persist immutable ownership per task, resolve its exact agent at execution, and initialize lifecycle under a retryable lock before running work.

## ISSUE-002: Domain failures are encoded as successful responses.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, not-found status, invalid logs, deletion failure, uninitialized components, capability errors, and the unknown-action branch can return payload.error. The handler wraps every normally returned payload in AgentResponse(ok=true), reserving transport failure only for raised exceptions.
- Suggested action: Map service results to typed wire failures; reserve ok=true for completed operations.

## ISSUE-003: Action parameters lack type, range, and response-size bounds.

- Dimension: `input_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, params is request.params or {} without a mapping check. Query, interval_hours, run_immediately, model/pipeline, task IDs, config fields, log type/history/offset/limit, and issue parameters pass without per-action schemas; TaskStore treats limit<=0 as read-all, allowing an arbitrarily large response.
- Suggested action: Use per-action schemas, bounded strings/enums/pagination, and a hard wire budget.

## ISSUE-004: No schedule/issue RPC handler test was found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, issue-runner and Gateway cron tests cover adjacent components, but no direct test calls this handler across its 13 actions. Scheduler startup/retry races, cross-channel/project agent ownership, error mapping, malformed params, unbounded logs, model-cache staleness, restart restore, and send behavior remain unverified.
- Suggested action: Add table-driven action, concurrency, restart, cross-project, failure, and bounded-log tests.

## ISSUE-005: Lazy scheduler publication exposes half-initialized or permanently poisoned service state.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, the handler assigns self._scheduler_service before awaiting start_scheduler and uses no initialization lock/state future. A concurrent request can observe the non-None service while startup is still running; if start_scheduler raises, the field remains non-None and later requests skip automatic startup retry. Read-only first requests also start autonomous polling before request-specific agent resolution.
- Suggested action: Initialize behind a shared lock/future, publish only after success, expose readiness, and clear/retry failed startup deterministically.

## ISSUE-006: Scheduled model selection can use stale or wrong same-name provider state.

- Dimension: `dependency_coupling`
- Severity: `high`
- Status: `open`
- Evidence: agentserver-schedule-auto-harness confirms create/run resolve models through AgentServer's name-only _model_cache. The cache has no config fingerprint or reload invalidation, can collapse same-name provider entries, and can remain partially initialized after constructor failure, yet task records retain only model_name.
- Suggested action: Persist a stable provider/model revision per task and rebuild an atomically versioned cache when configuration changes.
