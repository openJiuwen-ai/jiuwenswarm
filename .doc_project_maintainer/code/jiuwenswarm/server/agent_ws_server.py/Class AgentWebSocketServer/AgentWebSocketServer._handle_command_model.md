---
symbol: AgentWebSocketServer._handle_command_model
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_model(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:43Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:c00da85bb512fa02bdb985530f474c6e45c92c902a1c44beb7809979d495f5f0
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: medium
    status: open
    summary: "add_model and list behavior are stubs on the direct AgentServer path."
    evidence: "Current add_model only string/strips target, logs it, and returns model_added even when blank; it. See AgentWebSocketServer._handle_command_model/risks.md#issue-001."
    suggested_action: "Route through the gateway handler or implement its durable config contract."
  - id: ISSUE-002
    dimension: error_handling
    severity: high
    status: open
    summary: "switch_model can report applied=true after partial failure."
    evidence: "Current switch loops over arbitrary env_updates into os.environ, swallows cache-clear and reload. See AgentWebSocketServer._handle_command_model/risks.md#issue-002."
    suggested_action: "Propagate reload failure or return applied=false/partial status, and validate env_updates before arbitrary global env."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "The direct AgentServer switch_model path lacks coverage."
    evidence: "test_agentserver_cli_commands.py directly covers only no-action status and add_model. Switch success. See AgentWebSocketServer._handle_command_model/risks.md#issue-003."
    suggested_action: "Add direct switch_model success, validation, and reload-failure tests."
  - id: ISSUE-004
    dimension: boundary_safety
    severity: high
    status: open
    summary: "switch_model logging can expose API credentials."
    evidence: "The switch log comprehension masks only the exact key API_KEY; VIDEO_API_KEY, VISION_API_KEY. See AgentWebSocketServer._handle_command_model/risks.md#issue-004."
    suggested_action: "Use the shared sensitive-field masker before logging env_updates."
  - id: ISSUE-005
    dimension: state_mutation
    severity: high
    status: open
    summary: "Process-global model switching has no serialization or rollback."
    evidence: "The connection layer runs requests concurrently, while this method mutates os.environ key-by-key, clears. See AgentWebSocketServer._handle_command_model/risks.md#issue-005."
    suggested_action: "Serialize model switches and apply validated updates transactionally, restoring the previous environment/cache state on."
---

# AgentWebSocketServer._handle_command_model

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_model/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_model/risks.md)
