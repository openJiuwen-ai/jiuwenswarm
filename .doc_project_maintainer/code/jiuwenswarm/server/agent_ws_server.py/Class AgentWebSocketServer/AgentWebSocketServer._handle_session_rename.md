---
symbol: AgentWebSocketServer._handle_session_rename
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_rename(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: missing
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:37:49Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:55ad37b94c6c12fbef5c4deec4ae51f6cbe8b5db6b38bd2b4589f244c48fa8a6
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Unvalidated target session ids can escape the session storage root."
    evidence: "Current handler passes request.params directly to apply_session_rename. That helper lets. See AgentWebSocketServer._handle_session_rename/risks.md#issue-001."
    suggested_action: "Validate the effective id in the shared helper, reject absolute/traversal paths, and require the resolved metadata path."
  - id: ISSUE-002
    dimension: output_contract
    severity: medium
    status: open
    summary: "A successful response does not guarantee that the renamed title reached durable metadata."
    evidence: "For set/clear, apply_session_rename calls update_session_metadata, which updates _METADATA_CACHE and. See AgentWebSocketServer._handle_session_rename/risks.md#issue-002."
    suggested_action: "Define whether success means cache acceptance or durability; for durability, await or acknowledge the metadata write."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Missing direct rename-handler and helper tests."
    evidence: "Current repository search found no test reference to session.rename, apply_session_rename. See AgentWebSocketServer._handle_session_rename/risks.md#issue-003."
    suggested_action: "Add focused tests for query, set, clear, missing session_id/BAD_REQUEST, non-dict params, and WebSocket response."
---

# AgentWebSocketServer._handle_session_rename

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_rename/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_rename/risks.md)
