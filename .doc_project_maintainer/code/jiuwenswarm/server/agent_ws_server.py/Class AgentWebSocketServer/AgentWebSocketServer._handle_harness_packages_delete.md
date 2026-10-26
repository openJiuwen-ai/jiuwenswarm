---
symbol: AgentWebSocketServer._handle_harness_packages_delete
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_harness_packages_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: flawed
  state_mutation: shared
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: high
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:40:15Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:756f81a418c5cb2419a203e33ce6883b6f981e7661be6452399f4ad1aa556db0
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Deletion trusts persisted runtime_path without containment validation."
    evidence: "See AgentWebSocketServer._handle_harness_packages_delete/risks.md#issue-001."
    suggested_action: "Validate records on load and again before use; req."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "The RPC reports success after filesystem, unload, or persistence failu."
    evidence: "See AgentWebSocketServer._handle_harness_packages_delete/risks.md#issue-002."
    suggested_action: "Propagate structured per-stage failures, serialize."
  - id: ISSUE-003
    dimension: state_mutation
    severity: high
    status: open
    summary: "Global deletion does not reliably unload every active consumer."
    evidence: "See AgentWebSocketServer._handle_harness_packages_delete/risks.md#issue-003."
    suggested_action: "Enumerate and confirm unload from every active cha."
  - id: ISSUE-004
    dimension: boundary_safety
    severity: high
    status: open
    summary: "The destructive global RPC has no server-side authorization boundary."
    evidence: "See AgentWebSocketServer._handle_harness_packages_delete/risks.md#issue-004."
    suggested_action: "Require an authenticated administrator/package own."
  - id: ISSUE-005
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "Deletion can create an expensive agent before it knows whether one is."
    evidence: "See AgentWebSocketServer._handle_harness_packages_delete/risks.md#issue-005."
    suggested_action: "Load and validate package metadata first; only ins."
  - id: ISSUE-006
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Metadata and recursive deletion I/O block the AgentServer event loop."
    evidence: "See AgentWebSocketServer._handle_harness_packages_delete/risks.md#issue-006."
    suggested_action: "Move bounded filesystem work to a worker/job, enfo."
  - id: ISSUE-007
    dimension: test_coverage
    severity: high
    status: open
    summary: "No deletion handler or service test was found."
    evidence: "See AgentWebSocketServer._handle_harness_packages_delete/risks.md#issue-007."
    suggested_action: "Add handler/service/Gateway-fallback tests for suc."
---

# AgentWebSocketServer._handle_harness_packages_delete

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_harness_packages_delete/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_harness_packages_delete/risks.md)
