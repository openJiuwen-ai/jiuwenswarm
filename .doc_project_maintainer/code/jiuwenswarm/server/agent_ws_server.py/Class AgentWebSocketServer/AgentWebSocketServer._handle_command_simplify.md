---
symbol: AgentWebSocketServer._handle_command_simplify
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_simplify(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: weak
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: low
  test_coverage: partial
  observability: clear
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:43Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:274721f505c5bd783bb757de1b457c8e6755d046c9009e1bf97fbc89dbb9666d
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: low
    status: open
    summary: "Missing boundary and failure-path tests for a cross-boundary command handler."
    evidence: "At HEAD 39feee89, two direct tests cover base prompt shape and a short target. Gateway/TUI forwarding, _handle_message dispatcher selection, non-dict params, non-string/None targets, oversized targets, prompt-builder failure, and send failure are untested."
    suggested_action: "Add one routed request test plus malformed-param and error cases."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "target is stringified and appended without a size bound."
    evidence: "At HEAD 39feee89, params is assumed dict-like and str(params.get('target', '')) is stripped and appended verbatim. Explicit None becomes the literal Additional Focus value 'None', structured values become Python representations, and no byte/character bound protects either the response frame or the subsequent hidden model request."
    suggested_action: "Require a string, set a byte/character limit, and return a stable validation error when exceeded."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_command_simplify`

## Actual Role

Acts as a prompt-construction RPC rather than executing simplification. It assumes params is mapping-like, stringifies and trims optional `target`, appends that value under `Additional Focus` to the static three-phase reuse/quality/efficiency review template, and returns the full prompt in one locked unary response. The frontend is responsible for code-mode gating and for resubmitting the prompt as a hidden user message to an agent; this method never inspects a repository, launches reviewers, or applies fixes itself.

## Key Signals

- Input: Optional `params.target`; params type is implicit, and every target value is accepted through `str(...)` rather than a string contract.
- Output: One response with `payload.prompt`, or a raw error payload if parameter access or prompt construction fails; request metadata is not copied.
- Main side effects: Allocates a potentially large prompt, logs caught construction errors, and sends one WebSocket frame. Repository review/mutation occurs only in the later agent request.
- Main risk: An unbounded or structured target is embedded into both the response and downstream hidden model prompt, increasing frame/token cost and producing surprising focus text such as `None`.
- Related tests: Direct tests cover the static prompt and a normal target. Routing/forwarding, malformed or oversized input, builder failure, and send failure remain uncovered. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
