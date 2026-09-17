---
symbol: AgentWebSocketServer._handle_command_chrome
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_chrome(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: partial
  input_contract: implicit
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: low
  test_coverage: missing
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:14Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:b68f735421d9c54bd715e065874c7098855771edd0db4bfb4832f5e05c8f7a47
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: medium
    status: open
    summary: "Reports success with an empty payload without executing or describing a Chrome action."
    evidence: "Lines 3458-3464 unconditionally build ok=true with payload={} and never inspect request.params or invoke. See AgentWebSocketServer._handle_command_chrome/risks.md#issue-001."
    suggested_action: "Implement the intended Chrome behavior or make the route an explicit no-op/status acknowledgement."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "The frontend chrome command file appears disconnected from builtin command registration."
    evidence: "chrome.ts exports createChromeCommand and Gateway/AgentServer retain the command.chrome forwarding. See AgentWebSocketServer._handle_command_chrome/risks.md#issue-002."
    suggested_action: "Register the command path intentionally, or remove the dead builtin and forwarding surface."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct handler, dispatch, or frontend registration tests cover command.chrome."
    evidence: "Static searches found no direct test for _handle_command_chrome, ReqMethod.COMMAND_CHROME dispatch. See AgentWebSocketServer._handle_command_chrome/risks.md#issue-003."
    suggested_action: "Add tests for dispatch, response payload semantics, and command registration."
  - id: ISSUE-004
    dimension: error_handling
    severity: medium
    status: open
    summary: "The exception guard excludes the meaningful failure points."
    evidence: "Lines 3458-3472 wrap only dataclass response construction; encoding and send_wire_payload execute. See AgentWebSocketServer._handle_command_chrome/risks.md#issue-004."
    suggested_action: "Remove the ineffective local guard and rely on the explicit outer transport policy, or scope error handling around the."
---

# AgentWebSocketServer._handle_command_chrome

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_chrome/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_chrome/risks.md)
