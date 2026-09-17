---
symbol: AgentWebSocketServer._attach_landlock_status
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_attach_landlock_status(self, payload: dict[str, Any]) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: isolated
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
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
    dimension: output_contract
    severity: medium
    status: open
    summary: "An unavailable health endpoint is reported as Landlock unsupported."
    evidence: "fetch_health returns None for timeout, transport/non-200/JSON errors; this method converts all to supported=false, identical to a healthy unsupported service."
    suggested_action: "Expose probe availability separately; reserve false for a valid health response."
  - id: ISSUE-002
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Every successful sandbox command waits synchronously for a fresh health request."
    evidence: "_handle_command_sandbox awaits this before every successful response, including config-only changes and disable; fetch_health allows a 2-second timeout."
    suggested_action: "Cache capability data or skip probes for known-stopped/disabled endpoints."
  - id: ISSUE-003
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "Capability and compatibility can describe different effective services or policies."
    evidence: "supported comes from the remote endpoint, while compatibility comes from a local policy_file. External jiuwenbox manages its own policy and /health does not identify it."
    suggested_action: "Report effective policy remotely or label compatibility as local expected state."
  - id: ISSUE-004
    dimension: error_handling
    severity: medium
    status: open
    summary: "Unexpected attachment failures silently change the success payload shape."
    evidence: "The broad except only warns and leaves landlock absent; the caller still sends ok=true without a degraded marker."
    suggested_action: "Always emit a stable object with availability/error state."
  - id: ISSUE-005
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No AgentServer tests cover Landlock status attachment."
    evidence: "No test covers this helper, fetch_health through command.sandbox, or Landlock payload behavior; jiuwenbox tests only check its own health field."
    suggested_action: "Test healthy, unreachable, malformed, external, disabled, timeout, and degraded cases."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._attach_landlock_status`

## Actual Role

Selects a jiuwenbox endpoint, awaits `/health`, reads local policy compatibility, and adds both values to the payload. Probe failure becomes `supported: false`; unexpected failures can leave the field absent.

## Key Signals

- Input: Mutable sandbox response payload, optionally containing `jiuwenbox.host` and `port`.
- Output: Returns `None`; normally writes `payload["landlock"]`.
- Side effects: One HTTP health request, one possible local policy read, payload mutation, and warning logs.
- Main risk: Operator-visible capability state conflates unavailable with unsupported and can delay every sandbox response.
- Tests/flow: No direct/caller test or dedicated flow; sandbox runtime is pending in the build plan.

## Detail Index

- Detail docs pending.
