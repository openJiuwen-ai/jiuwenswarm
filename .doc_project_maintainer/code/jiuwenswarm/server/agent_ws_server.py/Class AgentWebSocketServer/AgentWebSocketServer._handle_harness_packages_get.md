---
symbol: AgentWebSocketServer._handle_harness_packages_get
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_harness_packages_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: clear
  output_contract: weak
  side_effects: implicit
  error_handling: partial
  state_mutation: external
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:40:13Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:b89469c093ff7138db553d0f507b086183d38ee8555af1fc00e83ae37f7add86
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "A GET can replace corrupt package state with a fresh inactive scan."
    evidence: "At HEAD 39feee89, get_packages_info delegates to load_packages, whose broad read/JSON fallback scans. See AgentWebSocketServer._handle_harness_packages_get/risks.md#issue-001."
    suggested_action: "Separate missing bootstrap from read failure; back up corrupt state and require explicit repair."
  - id: ISSUE-002
    dimension: output_contract
    severity: medium
    status: open
    summary: "Parsed package metadata is not schema-validated before success."
    evidence: "At HEAD 39feee89, _load_packages_no_fallback returns the decoded JSON value without enforcing the. See AgentWebSocketServer._handle_harness_packages_get/risks.md#issue-002."
    suggested_action: "Validate top-level and package-state fields; return a typed error."
  - id: ISSUE-003
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Construction blocks the loop and gives GET implicit mutations."
    evidence: "At HEAD 39feee89, AutoHarnessService is constructed on the event-loop thread before asyncio.to_thread.. See AgentWebSocketServer._handle_harness_packages_get/risks.md#issue-003."
    suggested_action: "Use a lightweight read-only repository or reused service; move I/O off-loop and surface persistence failures."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No direct handler or package-info contract tests were found."
    evidence: "At HEAD 39feee89, no direct get_packages_info, HARNESS_PACKAGES_GET, harness.packages.get, or handler. See AgentWebSocketServer._handle_harness_packages_get/risks.md#issue-004."
    suggested_action: "Test valid/missing/corrupt/wrong-shape data, persistence/init failure, routing, and response envelopes."
---

# AgentWebSocketServer._handle_harness_packages_get

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_harness_packages_get/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_harness_packages_get/risks.md)
