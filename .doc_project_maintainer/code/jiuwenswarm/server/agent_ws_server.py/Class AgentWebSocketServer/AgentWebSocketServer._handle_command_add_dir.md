---
symbol: AgentWebSocketServer._handle_command_add_dir
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_add_dir(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: implicit
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:13Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:6fd844903b9226bef5d907ed310663b9ebdd2be9ff27b652fa1ff0149e8e6aa7
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Durable trust mutation accepts any non-empty path from the command route."
    evidence: "_handle_message routes by COMMAND_ADD_DIR without a handler-local caller/channel authorization check.. See AgentWebSocketServer._handle_command_add_dir/risks.md#issue-001."
    suggested_action: "Gate this command by channel, auth, or permission policy and validate intended directory scope/existence before."
  - id: ISSUE-002
    dimension: input_contract
    severity: medium
    status: open
    summary: "remember is echoed but ignored."
    evidence: "Current handler reads remember with default false, but every accepted path calls. See AgentWebSocketServer._handle_command_add_dir/risks.md#issue-002."
    suggested_action: "Honor remember=false as non-persistent behavior, or remove/document it as response metadata only."
  - id: ISSUE-003
    dimension: output_contract
    severity: low
    status: fixed
    summary: "The handler no longer performs a blocking or best-effort agent reload."
    evidence: "Current method contains no get_config/reload_agents_config path after persistence.. See AgentWebSocketServer._handle_command_add_dir/risks.md#issue-003."
    suggested_action: "No handler change required; retain the non-reload regression test."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Direct tests do not cover real YAML writes, invalid inputs, or authorization boundaries."
    evidence: "Two direct tests stub persistence and cover success payload plus fixed no-reload behavior. See AgentWebSocketServer._handle_command_add_dir/risks.md#issue-004."
    suggested_action: "Add empty/non-dict input, helper failure, temp-config persistence, send-failure, and unauthorized-routing tests."
---

# AgentWebSocketServer._handle_command_add_dir

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_add_dir/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_add_dir/risks.md)
