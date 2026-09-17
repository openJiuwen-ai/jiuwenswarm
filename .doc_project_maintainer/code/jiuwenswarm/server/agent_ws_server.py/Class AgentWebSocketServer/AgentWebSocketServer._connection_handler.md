---
symbol: AgentWebSocketServer._connection_handler
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_connection_handler(self, ws: Any) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: clear
  performance_risk: high
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:37:45Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:ca59500c0905ec1edfc92c348321cd54e94f4410eb6c4eb3c8716e4f39f8bf98
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "Any connection close clears global active-connection state and cancels global work."
    evidence: "Current source assigns _current_ws/_current_send_lock before ack; every connection's finally. See AgentWebSocketServer._connection_handler/risks.md#issue-001."
    suggested_action: "Guard cleanup by active-slot ownership, or deliberately reject replacement connections."
  - id: ISSUE-002
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Scheduler shutdown is tied to per-connection cleanup."
    evidence: "The current per-connection finally block calls _stop_scheduler even though the scheduler is server state. See AgentWebSocketServer._connection_handler/risks.md#issue-002."
    suggested_action: "Move scheduler stop to server shutdown, or define it as connection-scoped and test that contract."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "connection.ack ordering can race with server push."
    evidence: "_current_ws is published before send_wire_payload(connection.ack), and ack does not use send_lock. See AgentWebSocketServer._connection_handler/risks.md#issue-003."
    suggested_action: "Send ack under the same lock before publishing _current_ws, or gate send_push until ack completes."
  - id: ISSUE-004
    dimension: error_handling
    severity: medium
    status: open
    summary: "Completed request-task exceptions are not retrieved."
    evidence: "task.add_done_callback(tasks.discard) removes completed tasks without reading exceptions.. See AgentWebSocketServer._connection_handler/risks.md#issue-004."
    suggested_action: "Retrieve and log task exceptions in the callback, or keep tasks until gather consumes them."
  - id: ISSUE-005
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Inbound frames create unbounded concurrent request tasks."
    evidence: "Each frame immediately calls asyncio.create_task and adds it to a set; there is no per-connection task. See AgentWebSocketServer._connection_handler/risks.md#issue-005."
    suggested_action: "Bound concurrent request tasks and pause or reject reads when the connection reaches that limit."
---

# AgentWebSocketServer._connection_handler

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._connection_handler/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._connection_handler/risks.md)
