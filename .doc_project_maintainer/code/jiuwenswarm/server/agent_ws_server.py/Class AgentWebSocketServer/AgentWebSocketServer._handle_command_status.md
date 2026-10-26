---
symbol: AgentWebSocketServer._handle_command_status
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_status(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
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
  side_effects: hidden
  error_handling: partial
  state_mutation: shared
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
  audited_symbol_hash: sha256:f9d955bbee18f19ab87cf058707665ad1abc9b7dec6ecded95e167b501282a0d
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: high
    status: open
    summary: "usage.models_used reports session modes rather than models."
    evidence: "Current usage branch groups each metadata record's mode into model_counts and emits those keys as. See AgentWebSocketServer._handle_command_status/risks.md#issue-001."
    suggested_action: "Aggregate model identity or rename the field to modes_used."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Usage aggregates are silently truncated to the newest 500 sessions."
    evidence: "Current code calls get_all_sessions_metadata(limit=500, offset=0), computes. See AgentWebSocketServer._handle_command_status/risks.md#issue-002."
    suggested_action: "Aggregate all pages or expose sample scope/truncation explicitly."
  - id: ISSUE-003
    dimension: side_effects
    severity: medium
    status: open
    summary: "The overview read path mutates memory cache and performs synchronous filesystem discovery."
    evidence: "Every overview calls clear_project_memory_cache(workspace_dir), then synchronously. See AgentWebSocketServer._handle_command_status/risks.md#issue-003."
    suggested_action: "Use cache-aware diagnostics off the event loop; do not clear shared cache for status."
  - id: ISSUE-004
    dimension: error_handling
    severity: medium
    status: open
    summary: "Memory diagnostic failure is indistinguishable from a clean result."
    evidence: "The overview catches all memory diagnostic exceptions, logs a warning, assigns memory_warnings=[], and. See AgentWebSocketServer._handle_command_status/risks.md#issue-004."
    suggested_action: "Return diagnostic availability/error metadata separately from an empty warning list."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No direct status-handler tests were found."
    evidence: "Repository search finds no direct _handle_command_status invocation or payload assertion; the only. See AgentWebSocketServer._handle_command_status/risks.md#issue-005."
    suggested_action: "Test all actions, >500 sessions, model aggregation, memory failures, encoding, and locking."
---

# AgentWebSocketServer._handle_command_status

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_status/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_status/risks.md)
