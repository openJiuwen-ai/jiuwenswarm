---
symbol: AgentWebSocketServer._handle_command_session
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_session(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: implicit
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: low
  test_coverage: partial
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:45Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:7bffdf215a2248dc3d4ae1c4208c0ac99fabc6ee53a35183a239ffa25c67e650
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Production command.session returns a hard-coded mock handoff."
    evidence: "Lines 5248-5257 fabricate sess_mock, an example.com URL, and session:<id> QR text without creating, locating, authorizing, or exposing a real session. The established TUI flow forwards command.session without a local replacement, so this payload reaches the client."
    suggested_action: "Integrate a configured authenticated handoff service and return its expiring URL/QR payload, or reject the command explicitly while the capability is unsupported."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Missing and arbitrary session IDs are accepted as successful handoffs."
    evidence: "AgentRequest.session_id is an unconstrained optional string. Line 5248 substitutes sess_mock when absent, and lines 5254-5256 interpolate any supplied value directly into URL-path and QR semantics without existence checks, authorization, validation, or URL encoding."
    suggested_action: "Require a non-empty existing session ID, authorize it for the caller, and encode opaque identifiers when constructing external representations."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "The sole direct test codifies the mock instead of the handoff contract."
    evidence: "The sole direct handler test asserts the exact example.com/session/sess_demo response. No test covers missing, unknown, unauthorized, or delimiter-bearing IDs, real token provisioning, expiry, or Gateway rendering."
    suggested_action: "Replace the fixture with contract tests for authenticated handoff creation plus invalid-ID, authorization, encoding, provider-failure, and end-to-end Gateway cases."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_command_session`

## Actual Role

Builds a synthetic session-handoff response from the request session ID (or `sess_mock`), encodes it as an E2A-compatible response, and sends it under the connection lock. It does not consult session state, provision remote access, create an authorization token, or generate a real QR artifact.

## Key Signals

- Caller: `_handle_message` dispatches `ReqMethod.COMMAND_SESSION` directly to this handler.
- Gateway path: TUI forwards `command.session` and explicitly has no local handler, so this payload reaches the client unchanged.
- Output: `session_id`, a hard-coded `https://example.com/session/...` URL, and plain `session:...` QR text.
- Side effects: Only the WebSocket send; no session or handoff state is mutated.
- Tests/flow: One happy-path unit test locks in the mock response. Existing dispatch/session-lifecycle evidence contains no authenticated remote-handoff, token-expiry, or authorization lifecycle; no tests were run during this re-audit.

## Detail Index

- Detail docs pending.
