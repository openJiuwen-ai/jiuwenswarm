---
symbol: AgentWebSocketServer._handle_harness_packages_deactivate
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_harness_packages_deactivate(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: partial
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:40:15Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:3fa4c2be52c5bc71cc7c191ed2d59bcea84b9a6f3434be11b5b7e6a75e8060c4
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Runtime and durable deactivation can diverge while success is returned."
    evidence: "Current deactivate_package catches and logs selected-agent unload failures, while AgentManager catches. See AgentWebSocketServer._handle_harness_packages_deactivate/risks.md#issue-001."
    suggested_action: "Collect fanout outcomes and commit durably as one serialized transaction, with compensation or explicit partial failure."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Global activation metadata is reconciled only in one channel."
    evidence: "Activation state lives in the single user-workspace harness-packages.json, but the handler always passes. See AgentWebSocketServer._handle_harness_packages_deactivate/risks.md#issue-002."
    suggested_action: "For global state fan out to all channels, or persist activation per channel/project."
  - id: ISSUE-003
    dimension: output_contract
    severity: medium
    status: open
    summary: "Unknown package IDs are reported as successful no-ops."
    evidence: "Current deactivate_package checks membership in active_package_ids before find_package_by_id. Any. See AgentWebSocketServer._handle_harness_packages_deactivate/risks.md#issue-003."
    suggested_action: "Validate package existence first, then distinguish already inactive."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No deactivation handler or service tests were found."
    evidence: "Repository search finds no test invoking deactivate_package, HARNESS_PACKAGES_DEACTIVATE. See AgentWebSocketServer._handle_harness_packages_deactivate/risks.md#issue-004."
    suggested_action: "Test success, partial failures, scope, IDs, identity, persistence, routing, and wire responses."
---

# AgentWebSocketServer._handle_harness_packages_deactivate

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_harness_packages_deactivate/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_harness_packages_deactivate/risks.md)
