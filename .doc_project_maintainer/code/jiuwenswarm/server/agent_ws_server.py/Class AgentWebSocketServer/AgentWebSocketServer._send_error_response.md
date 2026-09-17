---
symbol: AgentWebSocketServer._send_error_response
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_send_error_response(ws: Any, request: AgentRequest, send_lock: asyncio.Lock, error: str, code: str | None = None) -> dict[str, Any]"
health:
  overall: watch
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: safe
  input_contract: clear
  output_contract: clear
  side_effects: none
  error_handling: clear
  state_mutation: none
  dependency_coupling: low
  test_coverage: missing
  observability: not_applicable
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:07Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:c09ffc42bfa6c2fd7b60d9ba6e9ce637abc4756729f51937fea593832e1f5dcc
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: name_behavior_match
    severity: low
    status: open
    summary: "Name and transport parameters imply sending, but the helper only builds a wire dict."
    evidence: "Current lines 2688-2705 never read ws or send_lock and return encode_agent_response_for_wire(...) directly. The docstring is now accurate ('Build'), but all five callers must separately acquire send_lock and call send_wire_payload."
    suggested_action: "Rename to _build_error_response_wire and remove unused transport arguments, or make the helper async and own the locked send."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct regression tests cover helper output."
    evidence: "No test references _send_error_response or exercises its five early-return call sites in _handle_session_rewind_full/_handle_session_rewind_context. Shared wire-codec tests do not prove optional code omission, metadata propagation, or callsite locking/sending."
    suggested_action: "Add direct dict-contract tests plus handler tests for missing/invalid turn_index and unavailable rewind agent."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._send_error_response`

## Actual Role

Builds a failed `AgentResponse` from request identity/metadata, error text, and an optional code, then converts it to an E2A wire dictionary. Five validation or unavailable-agent branches in the full/context rewind handlers use it before separately locking and calling `send_wire_payload`. Despite its name and transport parameters, this static helper neither serializes to text nor sends.

## Key Signals

- Input: `AgentRequest` identity/metadata, an error string, and optional code.
- Output: E2A failed-response `dict`; payload always has `error` and includes `code` only when truthy, while request/channel IDs and metadata are preserved.
- Main side effects: None in this helper; the shared encoder may log conversion/fallback diagnostics. `ws` and `send_lock` are unused.
- Main risk: name/signature suggest sending, so every caller must remember the separate locked transport step.
- Test evidence: no direct helper or rewind early-return tests were found; no tests were run for this documentation-only re-audit.
- Related flow: `agentserver-session-lifecycle` covers rewind state but does not define this small wire-builder contract.

## Detail Index

- Detail docs pending.
