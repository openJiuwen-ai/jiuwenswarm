---
symbol: AgentWebSocketServer._handle_harness_packages_activate
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_harness_packages_activate(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: medium
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: flawed
  state_mutation: shared
  dependency_coupling: high
  test_coverage: missing
  observability: weak
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:40:14Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:e2d8f86b504cf062f1a128edd42cc7b562901c4071c8c35a70d0fc6f4a3ce292
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Activation can split agent and metadata state."
    evidence: "See AgentWebSocketServer._handle_harness_packages_activate/risks.md#issue-001."
    suggested_action: "Serialize activation with preflight, per-target ou."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "Success can lack runtime or durable state."
    evidence: "See AgentWebSocketServer._handle_harness_packages_activate/risks.md#issue-002."
    suggested_action: "Reject or queue runtime-less activation, propagate."
  - id: ISSUE-003
    dimension: state_mutation
    severity: high
    status: open
    summary: "Concurrent mutations can lose activation updates."
    evidence: "See AgentWebSocketServer._handle_harness_packages_activate/risks.md#issue-003."
    suggested_action: "Use cross-process locking, revisions, and atomic r."
  - id: ISSUE-004
    dimension: error_handling
    severity: medium
    status: open
    summary: "Runtime failures become client validation errors."
    evidence: "See AgentWebSocketServer._handle_harness_packages_activate/risks.md#issue-004."
    suggested_action: "Preserve typed errors with stable codes and saniti."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No activation lifecycle test was found."
    evidence: "See AgentWebSocketServer._handle_harness_packages_activate/risks.md#issue-005."
    suggested_action: "Test no-agent/write/fanout failures, rollback, con."
  - id: ISSUE-006
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Package existence is checked only after get_agent may create runtime s."
    evidence: "See AgentWebSocketServer._handle_harness_packages_activate/risks.md#issue-006."
    suggested_action: "Resolve and validate the package/config first, the."
  - id: ISSUE-007
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Service construction and package metadata I/O remain on the event loop."
    evidence: "See AgentWebSocketServer._handle_harness_packages_activate/risks.md#issue-007."
    suggested_action: "Use a reused async package repository with seriali."
---

# AgentWebSocketServer._handle_harness_packages_activate

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_harness_packages_activate/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_harness_packages_activate/risks.md)
