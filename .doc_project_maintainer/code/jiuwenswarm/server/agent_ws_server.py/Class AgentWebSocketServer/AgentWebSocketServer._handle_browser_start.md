---
symbol: AgentWebSocketServer._handle_browser_start
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_browser_start(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: mixed
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: implicit
  output_contract: weak
  side_effects: implicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:46Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:83492fc9df7a7e91f6b08d9bc6c21ce4068df15aac6e4ba6c314394170555023
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: high
    status: open
    summary: "Process creation is reported as browser readiness."
    evidence: "See AgentWebSocketServer._handle_browser_start/risks.md#issue-001 for full evidence."
    suggested_action: "Probe bounded CDP readiness; return pid/endpoint/status and terminate failed."
  - id: ISSUE-002
    dimension: state_mutation
    severity: high
    status: open
    summary: "Browser start is neither idempotent nor lifecycle-managed."
    evidence: "See AgentWebSocketServer._handle_browser_start/risks.md#issue-002 for full evidence."
    suggested_action: "Use a singleton runtime manager with serialized ensure-running and explicit."
  - id: ISSUE-003
    dimension: error_handling
    severity: high
    status: open
    summary: "Post-spawn failure can return an error while leaving an orphan browser."
    evidence: "See AgentWebSocketServer._handle_browser_start/risks.md#issue-003 for full evidence."
    suggested_action: "Retain the child handle for rollback and expose partial-state diagnostics."
  - id: ISSUE-004
    dimension: boundary_safety
    severity: high
    status: open
    summary: "The RPC can expose an unauthenticated CDP endpoint beyond loopback."
    evidence: "See AgentWebSocketServer._handle_browser_start/risks.md#issue-004 for full evidence."
    suggested_action: "Enforce loopback unless separately authorized for remote CDP exposure."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No handler, launcher, or end-to-end browser-start tests were found."
    evidence: "See AgentWebSocketServer._handle_browser_start/risks.md#issue-005 for full evidence."
    suggested_action: "Add fake-process tests and a Gateway-to-AgentServer lifecycle integration test."
  - id: ISSUE-006
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Synchronous launch work blocks the AgentServer event loop."
    evidence: "See AgentWebSocketServer._handle_browser_start/risks.md#issue-006 for full evidence."
    suggested_action: "Run the blocking launcher in a worker thread or replace it with an async."
---

# AgentWebSocketServer._handle_browser_start

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_browser_start/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_browser_start/risks.md)
