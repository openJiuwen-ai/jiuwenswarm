---
symbol: AgentWebSocketServer._handle_session_rewind_context
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_rewind_context(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: implicit
  output_contract: weak
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
  audited_at: 2026-07-14T11:38:08Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:2e2b50d3f897235d671a834cc252c00759006b3725bc18cc68867110aa8688a8
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "Non-atomic rewind can leave history truncated after context failure."
    evidence: "Current handler calls synchronous rewind_session first; it truncates history, queues metadata count. See AgentWebSocketServer._handle_session_rewind_context/risks.md#issue-001."
    suggested_action: "Add per-session transaction/rollback or explicit partial-state recovery."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "Context durability failure can still return ok=true."
    evidence: "The handler always builds ok=true after both calls and only exposes context_ok in payload, so. See AgentWebSocketServer._handle_session_rewind_context/risks.md#issue-002."
    suggested_action: "Fail or return explicit partial status unless context durability is confirmed."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: high
    status: open
    summary: "session_id is an unchecked path component."
    evidence: "target_sid is only str/strip-normalized from params or request and is passed to rewind_session; its. See AgentWebSocketServer._handle_session_rewind_context/risks.md#issue-003."
    suggested_action: "Validate the ID and enforce containment under sessions root."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct handler tests cover rewind_context."
    evidence: "Repository search finds rewind_session_context helper cases in test_compact_partial.py but no direct or. See AgentWebSocketServer._handle_session_rewind_context/risks.md#issue-004."
    suggested_action: "Test success, invalid input, no-agent, wire shape, and partial failures."
  - id: ISSUE-005
    dimension: output_contract
    severity: low
    status: open
    summary: "Error codes are inconsistent."
    evidence: "Missing/invalid params and ValueError use BAD_REQUEST; the no-agent response calls _send_error_response. See AgentWebSocketServer._handle_session_rewind_context/risks.md#issue-005."
    suggested_action: "Add stable codes such as AGENT_UNAVAILABLE and INTERNAL_ERROR."
---

# AgentWebSocketServer._handle_session_rewind_context

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_rewind_context/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_rewind_context/risks.md)
