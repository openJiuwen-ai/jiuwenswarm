---
symbol: AgentWebSocketServer._handle_sandbox_disable
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_sandbox_disable(self, channel_id: str) -> dict[str, Any]"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: clear
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: high
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  audited_symbol_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "Disable can persist before agent recreation is known to succeed."
    evidence: "Line 4710 writes enabled=false before recreate_agent. AgentManager catches per-mode rebuild failures at lines 603-615 and returns normally, after which this helper reports agent_recreated=true. With no channel agents it instead reaches agents.keys() on None and fails after persistence."
    suggested_action: "Return a recreation outcome and report degraded state, or roll back the runtime flag when recreation cannot be completed."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: medium
    status: open
    summary: "A failed jiuwenbox stop produces contradictory status."
    evidence: "Lines 4717-4724 swallow stop failure and retain jiuwenbox_stopped=false, but lines 4736-4738 still describe the owned endpoint as ready=false even though it may remain live."
    suggested_action: "Set ready from the stop result and expose the stop error or a degraded status."
  - id: ISSUE-003
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Disable can block the command response for up to 60 seconds."
    evidence: "The helper awaits JiuwenBoxRunner.stop inline; its owned-process shutdown waits up to 60 seconds at jiuwenbox_runner.py:479 before killing the process."
    suggested_action: "Return an accepted/stopping state and finish shutdown asynchronously, or align the bounded wait with the RPC timeout."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Sandbox disable has no direct regression coverage."
    evidence: "No test references command.sandbox, _handle_sandbox_disable, jiuwenbox_stopped, or agent_recreated; no JiuwenBoxRunner stop test was found."
    suggested_action: "Cover success, no-agent/rebuild failure, owned versus external runner, stop failure, and slow shutdown."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_sandbox_disable`

## Actual Role

Persists global sandbox runtime as disabled, recreates all active agents on the target channel so their system-operation cards drop sandbox mode, then stops a locally owned jiuwenbox process and returns runtime, recreation, process-stop, and endpoint status. External jiuwenbox instances are intentionally not stopped.

## Key Signals

- Input: Normalized channel ID supplied by the `command.sandbox` dispatcher.
- Output: A payload consumed by the outer handler, which attaches effective-file and Landlock status before sending the response.
- Side effects: Writes `config.yaml`, cleans and recreates channel agents, and may terminate the owned jiuwenbox subprocess.
- Main risk: Persistence, recreation, and process shutdown are not transactional; failure reporting can claim recreation/readiness states that were not achieved.
- Tests/flow: No direct sandbox-disable or runner-stop tests were found. No dedicated sandbox flow exists in project docs.

## Detail Index

- Detail docs pending.
