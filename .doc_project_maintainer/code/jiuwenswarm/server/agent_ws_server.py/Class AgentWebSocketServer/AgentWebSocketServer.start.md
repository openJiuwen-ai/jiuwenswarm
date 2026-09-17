---
symbol: AgentWebSocketServer.start
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "start(self) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: missing
  observability: clear
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:37:45Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:48d560314e29df8001354a8f0b89a30de39f338cde541b4eb77f5ba4530aed71
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Best-effort sandbox bootstrap can delay post-start initialization."
    evidence: "Current HEAD 39feee89 still binds and logs the listener before awaiting _bootstrap_internal_jiuwenbox().. See AgentWebSocketServer.start/risks.md#issue-001."
    suggested_action: "Run jiuwenbox bootstrap in a named background task, or bound it with a shorter startup-specific timeout if."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Missing direct lifecycle tests for start()."
    evidence: "tests/unit_tests/test_app_agentserver.py replaces get_instance() with a fake whose start() only appends. See AgentWebSocketServer.start/risks.md#issue-002."
    suggested_action: "Add async tests for normal start, duplicate-start no-op, legacy/fallback serve behavior, checkpointer failure."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "Cancellation during post-bind sandbox bootstrap can leave the listener open."
    evidence: "Current HEAD assigns self._server before awaiting _bootstrap_internal_jiuwenbox(); that helper catches. See AgentWebSocketServer.start/risks.md#issue-003."
    suggested_action: "Wrap post-bind initialization in cancellation cleanup that closes the bound server before re-raising, or move optional."
---

# AgentWebSocketServer.start

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer.start/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer.start/risks.md)
