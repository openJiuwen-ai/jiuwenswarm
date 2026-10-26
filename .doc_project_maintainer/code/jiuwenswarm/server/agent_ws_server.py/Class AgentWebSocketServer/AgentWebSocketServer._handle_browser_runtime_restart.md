---
symbol: AgentWebSocketServer._handle_browser_runtime_restart
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_browser_runtime_restart(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: clear
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: high
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:47Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:cd1bac49ada072a81ea4f62e74600b855dbf662785db11285f15f64f8cebccee
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: performance_risk
    severity: high
    status: open
    summary: "A browser restart can block the AgentServer event loop for tens of seconds."
    evidence: "Current async handler calls synchronous restart_local_browser_runtime_server directly. Its lifecycle can. See AgentWebSocketServer._handle_browser_runtime_restart/risks.md#issue-001."
    suggested_action: "Use asyncio.to_thread or an async dependency with a bounded timeout and cancellation-safe result handling."
  - id: ISSUE-002
    dimension: state_mutation
    severity: high
    status: open
    summary: "Concurrent restart requests race on process-global browser state."
    evidence: "_handle_message dispatches requests concurrently, while the restart dependency reads then. See AgentWebSocketServer._handle_browser_runtime_restart/risks.md#issue-002."
    suggested_action: "Serialize restart with a process-scoped async single-flight lock and make the global transition atomic."
  - id: ISSUE-003
    dimension: output_contract
    severity: high
    status: open
    summary: "The config-save caller treats a failed restart response as success."
    evidence: "The handler returns ok=false on exception, but current app_gateway config-save code awaits. See AgentWebSocketServer._handle_browser_runtime_restart/risks.md#issue-003."
    suggested_action: "Define explicit outcomes; make Gateway check ok and trigger its restart fallback on failure."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "The restart RPC and config-save integration have no regression coverage."
    evidence: "Repository search finds no test reference to _handle_browser_runtime_restart, BROWSER_RUNTIME_RESTART. See AgentWebSocketServer._handle_browser_runtime_restart/risks.md#issue-004."
    suggested_action: "Cover restart/no-op/failure, event-loop responsiveness, concurrency, response encoding, and Gateway failure propagation."
---

# AgentWebSocketServer._handle_browser_runtime_restart

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_browser_runtime_restart/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_browser_runtime_restart/risks.md)
