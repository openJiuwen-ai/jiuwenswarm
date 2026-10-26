---
symbol: AgentWebSocketServer._handle_harness_packages_scan
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_harness_packages_scan(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: watch
  input_contract: clear
  output_contract: weak
  side_effects: explicit
  error_handling: flawed
  state_mutation: persistent
  dependency_coupling: high
  test_coverage: missing
  observability: weak
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:40:13Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:f9e43242f5fd9ad918c7d1010d9b09549cb940cc74d27acfbdf8cdc08e817e6d
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Failed scans can overwrite valid state with partial data."
    evidence: "At HEAD 39feee89, every normal return from scan_runtime_extensions is passed directly to save_packages.. See AgentWebSocketServer._handle_harness_packages_scan/risks.md#issue-001."
    suggested_action: "Fail the scan, retain the prior snapshot, and save only validated results."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "Persistence failure is reported as RPC success."
    evidence: "At HEAD 39feee89, save_packages catches write exceptions internally rather than returning/raising a. See AgentWebSocketServer._handle_harness_packages_scan/risks.md#issue-002."
    suggested_action: "Raise or return write status and acknowledge only verified persistence."
  - id: ISSUE-003
    dimension: state_mutation
    severity: high
    status: open
    summary: "Package metadata uses an unlocked, non-atomic read-modify-write cycle."
    evidence: "At HEAD 39feee89, scan_runtime_extensions reads package/activation state in one worker call and. See AgentWebSocketServer._handle_harness_packages_scan/risks.md#issue-003."
    suggested_action: "Use cross-process locking, revisions, and temp-file atomic replace."
  - id: ISSUE-004
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Heavy service construction still runs on the event loop."
    evidence: "At HEAD 39feee89, AutoHarnessService is constructed before the first asyncio.to_thread call. Its. See AgentWebSocketServer._handle_harness_packages_scan/risks.md#issue-004."
    suggested_action: "Reuse a managed service or move construction off-loop."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No scan RPC or package-scan persistence test was found."
    evidence: "At HEAD 39feee89, no direct test references _handle_harness_packages_scan, HARNESS_PACKAGES_SCAN. See AgentWebSocketServer._handle_harness_packages_scan/risks.md#issue-005."
    suggested_action: "Test failures, partial scans, concurrent writers, atomicity, fallback parity, and loop responsiveness."
---

# AgentWebSocketServer._handle_harness_packages_scan

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_harness_packages_scan/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_harness_packages_scan/risks.md)
