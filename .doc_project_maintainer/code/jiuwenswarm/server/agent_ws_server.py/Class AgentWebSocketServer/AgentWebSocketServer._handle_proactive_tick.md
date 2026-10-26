---
symbol: AgentWebSocketServer._handle_proactive_tick
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_proactive_tick(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:10Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:770724d7e805a586a2fec33ae7fe03f4e4b457c106314652e3bdbb1d053033de
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Missing direct coverage for the websocket adapter and proactive cron branch."
    evidence: "ProactiveEngine flow tests cover delivery, cooldown, quota, and busy-session outcomes, but no direct. See AgentWebSocketServer._handle_proactive_tick/risks.md#issue-001."
    suggested_action: "Add fake-engine and fake-websocket tests for uninitialized, success, skipped, exception, target_channel pass-through."
  - id: ISSUE-002
    dimension: input_contract
    severity: low
    status: open
    summary: "params and target_channel are accepted implicitly from an Any boundary."
    evidence: "Canonical E2A conversion coerces params to dict, but legacy _payload_to_request preserves raw params. See AgentWebSocketServer._handle_proactive_tick/risks.md#issue-002."
    suggested_action: "Guard request.params as a dict and normalize target_channel with str.strip() to a non-empty string or None before."
  - id: ISSUE-003
    dimension: observability
    severity: medium
    status: open
    summary: "Engine failures are reported to Cron as benign no-recommendation skips."
    evidence: "ProactiveEngine.tick_now catches ordinary exceptions and returns False; lines 3051-3065 map every False. See AgentWebSocketServer._handle_proactive_tick/risks.md#issue-003."
    suggested_action: "Return a structured tick outcome or propagate engine failures so the handler and scheduler can distinguish failed from."
---

# AgentWebSocketServer._handle_proactive_tick

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_proactive_tick/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_proactive_tick/risks.md)
