---
symbol: AgentWebSocketServer._handle_permissions_config
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_permissions_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: implicit
  output_contract: partial
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: medium
  test_coverage: partial
  observability: weak
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:09Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:75e7b80fa8f90b6a67ccf4be74c748fcd236e1c57bd314a975206420e2a868d3
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Direct coverage is limited to the nonblocking mutation-reload path."
    evidence: "At HEAD 39feee89, test_handle_permissions_config_does_not_block_on_slow_reload covers only one mocked. See AgentWebSocketServer._handle_permissions_config/risks.md#issue-001."
    suggested_action: "Add async tests for read-only no-reload, mutation reload, dispatcher error no-reload, and reload-exception behavior."
  - id: ISSUE-002
    dimension: observability
    severity: medium
    status: open
    summary: "Mutation success is returned before the background reload outcome is known."
    evidence: "At HEAD 39feee89, a successful mutation schedules reload_agents_config and immediately sends the. See AgentWebSocketServer._handle_permissions_config/risks.md#issue-002."
    suggested_action: "Document eventual consistency, log at warning, or include response metadata when runtime permissions may be stale."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "No local authorization or channel gate protects permissions mutations."
    evidence: "At HEAD 39feee89, _handle_message routes solely by req_method membership and this handler delegates. See AgentWebSocketServer._handle_permissions_config/risks.md#issue-003."
    suggested_action: "Document trusted-Gateway assumptions or add an explicit admin/channel/session gate."
  - id: ISSUE-004
    dimension: error_handling
    severity: high
    status: open
    summary: "A post-mutation snapshot failure turns a committed change into an error response."
    evidence: "At HEAD 39feee89, dispatch_permissions_config_request can persist a mutation before get_config() is. See AgentWebSocketServer._handle_permissions_config/risks.md#issue-004."
    suggested_action: "Capture the reload snapshot before committing, or isolate post-commit scheduling failures and return an explicit."
  - id: ISSUE-005
    dimension: performance_risk
    severity: medium
    status: open
    summary: "The async WebSocket handler performs synchronous config I/O on the event loop."
    evidence: "At HEAD 39feee89, dispatch_permissions_config_request synchronously reads or rewrites YAML, and. See AgentWebSocketServer._handle_permissions_config/risks.md#issue-005."
    suggested_action: "Move config persistence and snapshot reads to an async service or worker thread, preserving serialized mutation."
---

# AgentWebSocketServer._handle_permissions_config

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_permissions_config/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_permissions_config/risks.md)
