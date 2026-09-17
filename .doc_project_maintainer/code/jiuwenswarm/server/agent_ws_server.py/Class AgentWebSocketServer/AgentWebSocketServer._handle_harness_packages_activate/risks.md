---
symbol: AgentWebSocketServer._handle_harness_packages_activate
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_harness_packages_activate audit evidence

## ISSUE-001: Activation can split agent and metadata state.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, activate_package loads the selected agent first, synchronously persists active status next, and then broadcasts to other single-agent variants. AgentManager catches per-session/per-agent fanout failures and returns no aggregate failure, so neither the already-loaded target nor metadata is rolled back.
- Suggested action: Serialize activation with preflight, per-target outcomes, rollback, and a defined success policy.

## ISSUE-002: Success can lack runtime or durable state.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, no-agent activation marks metadata and returns a success message without runtime application. Already-active metadata also short-circuits without verifying any live runtime, and save_packages suppresses write failures, so this handler can return ok=true without durable or applied state.
- Suggested action: Reject or queue runtime-less activation, propagate write failure, and return target status.

## ISSUE-003: Concurrent mutations can lose activation updates.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, update_active_status reloads and directly rewrites global package JSON without cross-request/process locking, revision checks, or atomic replace. Concurrent activation/deactivation/scan and Web fallback writers can overwrite each other's active_package_ids changes.
- Suggested action: Use cross-process locking, revisions, and atomic replacement.

## ISSUE-004: Runtime failures become client validation errors.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, activate_package wraps file, runtime load, persistence, and broadcast-originated exceptions into ValueError. This handler maps ValueError through a mostly validation-oriented code mapper and returns raw exception text, so operational failures can appear as client BAD_REQUEST-style errors.
- Suggested action: Preserve typed errors with stable codes and sanitized messages.

## ISSUE-005: No activation lifecycle test was found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, no direct test references the activate RPC, activate_package, or package-change broadcast. No-agent/already-active, write/fanout/load failure, invalid-ID agent creation, rollback, concurrency, error codes, fallback parity, and response paths are unverified; no dedicated flow exists.
- Suggested action: Test no-agent/write/fanout failures, rollback, concurrency, codes, and fallback parity.

## ISSUE-006: Package existence is checked only after get_agent may create runtime state.

- Dimension: `state_mutation`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, the handler validates package_id only for truthiness, then awaits AgentManager.get_agent, which may auto-create a mode/project-scoped agent, before AutoHarnessService loads metadata and discovers an unknown or invalid package. A rejected activation can therefore leave a newly created agent.
- Suggested action: Resolve and validate the package/config first, then select or create runtime only for a valid activation plan.

## ISSUE-007: Service construction and package metadata I/O remain on the event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, AutoHarnessService construction creates directories/config/scheduler state synchronously, and activate_package synchronously loads package JSON, checks paths, and later updates/saves metadata around awaited runtime calls. None of that filesystem work is offloaded.
- Suggested action: Use a reused async package repository with serialized atomic metadata operations, or move blocking construction/I/O to a worker.
