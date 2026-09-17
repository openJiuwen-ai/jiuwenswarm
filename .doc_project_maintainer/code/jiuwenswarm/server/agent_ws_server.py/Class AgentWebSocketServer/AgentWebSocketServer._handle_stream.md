---
symbol: AgentWebSocketServer._handle_stream
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_stream(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: high
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: low
audit:
  status: audit_expired
  auditor: codex
  audited_at: 2026-07-14T11:37:48Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:0f201b65eefd1358f01cf2a596973166607d2ae05d7d088411e035b929ed19e4
  confidence: confirmed
  expired_reason: "2026-08-01 source split into a foreground guard wrapper and _handle_stream_impl."
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Stream task registration can leak stale session entries before cleanup starts."
    evidence: "Current HEAD 39feee89 registers current_task in _session_stream_tasks at lines 2195-2197.. See AgentWebSocketServer._handle_stream/risks.md#issue-001."
    suggested_action: "Wrap registration, agent resolution, heartbeat setup, streaming, and cleanup in one outer try/finally."
  - id: ISSUE-002
    dimension: performance_risk
    severity: medium
    status: fixed
    summary: "Heartbeat loop no longer leaves pending wait tasks behind."
    evidence: "Current lines 2224-2227 directly await heartbeat_event.wait() through asyncio.wait_for; the prior. See AgentWebSocketServer._handle_stream/risks.md#issue-002."
    suggested_action: "No code change required; add a heartbeat lifecycle regression test when practical."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "Unexpected heartbeat failures can surface late and mask the stream outcome."
    evidence: "The heartbeat loop catches only cancellation and WebSocketConnectionClosed. Finalization cancels and. See AgentWebSocketServer._handle_stream/risks.md#issue-003."
    suggested_action: "Capture and log unexpected heartbeat exceptions, and prevent heartbeat teardown from replacing the primary stream."
  - id: ISSUE-004
    dimension: state_mutation
    severity: medium
    status: open
    summary: "One task slot per session cannot represent concurrent streams."
    evidence: "_session_stream_tasks[session_id] = current_task overwrites any existing stream for that session.. See AgentWebSocketServer._handle_stream/risks.md#issue-004."
    suggested_action: "Use request-keyed or per-session task sets, or atomically cancel/reject the predecessor before replacement; add a."
---

# AgentWebSocketServer._handle_stream

## Actual Role

Wraps stream handling with the warm-pool foreground guard for chat-like methods. It increments the foreground count before delegating to `_handle_stream_impl` and releases it in `finally`, so exceptions and oversized-output termination cannot leave background warming paused.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_stream/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_stream/risks.md)
