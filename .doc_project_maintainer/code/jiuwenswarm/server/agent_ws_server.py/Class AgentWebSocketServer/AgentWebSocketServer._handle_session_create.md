---
symbol: AgentWebSocketServer._handle_session_create
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_create(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: critical
  name_behavior_match: mismatch
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: hidden
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: audit_expired
  auditor: codex
  audited_at: 2026-07-14T11:39:39Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:33487351f0a252dd739869feacd05acef75371d18829a892b7f4d27be460572e
  confidence: confirmed
  expired_reason: "Implementation changed on 2026-07-31 for AgentServer-owned IDs, create_token idempotency, project-first validation, and warm-pool claims; no independent symbol health re-audit was performed."
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: critical
    status: open
    summary: "Untrusted explicit session IDs become authoritative filesystem identities."
    evidence: "Current code only strips a string before AgentManager returns every nonempty explicit value unchanged.. See AgentWebSocketServer._handle_session_create/risks.md#issue-001."
    suggested_action: "Centralize strict ID syntax/length validation and enforce resolved-path containment at each filesystem boundary."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Create neither persists nor uniquely reserves a session."
    evidence: "Current AgentManager.create_session only echoes a nonempty ID, generates acp_<8 hex> for ACP, or returns. See AgentWebSocketServer._handle_session_create/risks.md#issue-002."
    suggested_action: "Use one transport-independent service to validate/reserve identity, persist metadata atomically, and report conflicts."
  - id: ISSUE-003
    dimension: side_effects
    severity: high
    status: open
    summary: "Team creation can stop distributed runtimes before success is observable."
    evidence: "For resolved mode 'team', current code awaits TeamManager.prepare_session_switch before encoding or. See AgentWebSocketServer._handle_session_create/risks.md#issue-003."
    suggested_action: "Separate creation from switching; make switching recoverable and classify send failures before retrying."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Tests cover mocked success and one successful team switch only."
    evidence: "The three direct tests use FakeAgentManager/FakeTeamManager to assert generated/explicit IDs and one. See AgentWebSocketServer._handle_session_create/risks.md#issue-004."
    suggested_action: "Add real-manager contract, adversarial ID, persistence, switch-failure, and transport-failure tests."
---

# AgentWebSocketServer._handle_session_create

## Actual Role

Validates project/work-mode binding, rejects explicit new IDs, canonicalizes eligible single-Agent runtime mode from final `work_mode`, claims an AgentServer-owned warm or warming session, writes metadata, and returns prewarm status. Code sessions persist `code.normal` even when a legacy Channel sent `mode=agent`. The linked audit details describe the previous implementation and remain historical until re-audited.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_create/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_create/risks.md)
