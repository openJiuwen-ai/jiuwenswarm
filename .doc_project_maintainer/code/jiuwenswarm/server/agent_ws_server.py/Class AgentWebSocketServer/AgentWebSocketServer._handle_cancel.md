---
symbol: AgentWebSocketServer._handle_cancel
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_cancel(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, *, allow_create: bool = True) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:37:47Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:3edeec23f98743055e846cec892020626323db8b495263ecbdd3d128494dd144
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: medium
    status: fixed
    summary: "Missing team mode params could make team cancellation cleanup run after generic interrupt handling."
    evidence: "Current _handle_message lines 1624-1636 cancel the session-keyed stream task before calling. See AgentWebSocketServer._handle_cancel/risks.md#issue-001."
    suggested_action: "Retain the pre-dispatch session-task cancellation and its disconnect/manual cancel regression coverage."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Coverage exercises disconnect no-create behavior but not runtime selection."
    evidence: "Eight focused cancel cases in test_agent_ws_connection_close.py cover existing fake-agent dispatch. See AgentWebSocketServer._handle_cancel/risks.md#issue-002."
    suggested_action: "Add focused tests for exact-mode reuse, ambiguous/missing-mode selection, default creation, malformed params, and."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Cache fallback can deliver cancellation to the wrong mode runtime."
    evidence: "Lines 1797-1814 first look up requested mode/project, then retry get_agent_nowait(channel_id. See AgentWebSocketServer._handle_cancel/risks.md#issue-003."
    suggested_action: "Resolve cancellation by session-to-runtime ownership, or reject ambiguous fallback instead of selecting the first."
  - id: ISSUE-004
    dimension: side_effects
    severity: medium
    status: open
    summary: "A normal cancel can create a new agent when no runtime exists."
    evidence: "With default allow_create=True, two cache misses reach AgentManager.get_agent at lines 1836-1849, which. See AgentWebSocketServer._handle_cancel/risks.md#issue-004."
    suggested_action: "Default cancellation to no-create and return the existing acknowledgement unless a caller explicitly requires runtime."
---

# AgentWebSocketServer._handle_cancel

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_cancel/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_cancel/risks.md)
