---
symbol: AgentWebSocketServer._handle_command_context
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_context(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: implicit
  side_effects: implicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:41Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:0605f85e2e7e56fc1be3450f360e5857fb7c3ecc046c6fb7b004241426920792
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct command.context tests cover dispatch, routing, success payload, or error envelope."
    evidence: "Static search found nearby command and mode-resolution tests, but no test directly targets _handle_command_context, COMMAND_CONTEXT dispatch, or the frontend command payload contract."
    suggested_action: "Add direct handler tests for agent lookup args, successful payload forwarding, missing agent, and adapter exception responses."
  - id: ISSUE-002
    dimension: side_effects
    severity: medium
    status: open
    summary: "A read-only context query can initialize runtime adapters."
    evidence: "Lines 3641-3646 call AgentManager.get_agent, whose contract auto-creates missing agents; DeepAdapter.get_context_usage lines 7466-7471 may then call _get_or_create_session_adapter before reading usage."
    suggested_action: "Expose a non-creating lookup path or document and test initialization as part of the command contract."
  - id: ISSUE-003
    dimension: error_handling
    severity: low
    status: open
    summary: "Business failures have no stable machine-readable error contract."
    evidence: "Lines 3659-3666 collapse invalid request params, missing-agent, initialization, and adapter failures into ok=false with only the raw exception string. Encoding has its own fallback and send failures rely on _handle_message's outer policy."
    suggested_action: "Map expected lookup, validation, and adapter failures to stable error codes while retaining the outer transport-send policy."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_command_context`

## Actual Role

Handles `command.context` as a transport bridge: defaults the session/channel, resolves mode/sub-mode and project scope, obtains the scoped agent, delegates `get_context_usage(session_id=...)`, and returns the adapter's statistics dictionary unchanged. The lookup may initialize an agent and a session-scoped adapter even though the command is observational.

## Key Signals

- Input: `session_id`, optional `params.mode`, channel id, and request project directory.
- Output: One WebSocket response containing the adapter context-usage payload unchanged, or `ok=false` with a raw error string.
- Main side effects: May initialize runtime adapters, reads shared context state, and sends a WebSocket frame.
- Main risk: An observational request can create runtime state, while payload/error behavior is untested at this boundary.
- Related tests: Static search found no direct handler, dispatch, or frontend payload-contract tests; no tests were run during this re-audit.

## Detail Index

- Detail docs pending.
