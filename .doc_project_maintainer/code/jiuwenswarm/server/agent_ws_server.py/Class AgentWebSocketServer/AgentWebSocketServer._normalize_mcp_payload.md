---
symbol: AgentWebSocketServer._normalize_mcp_payload
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_normalize_mcp_payload(params: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: weak
  output_contract: implicit
  side_effects: none
  error_handling: clear
  state_mutation: none
  dependency_coupling: medium
  test_coverage: partial
  observability: not_applicable
  performance_risk: low
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: medium
    status: open
    summary: "Update normalization can drop supported MCP fields."
    evidence: "Whole-item upsert receives a whitelist payload that drops server_id and stdio timeout_s; HTTP timeout_s accepts bool/zero/negative values that the runtime builder ignores."
    suggested_action: "Reuse the runtime config schema, preserve supported fields, and require a positive non-bool timeout."
  - id: ISSUE-002
    dimension: input_contract
    severity: medium
    status: open
    summary: "enabled uses Python truthiness, so string false values become enabled."
    evidence: "The method uses bool(merged.get('enabled', True)); WebSocket callers can send strings even though the TUI normally sends booleans."
    suggested_action: "Accept only booleans or centrally parse true/false string forms and reject other types."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "HTTP/SSE URLs are only checked for non-empty text."
    evidence: "Invalid URLs can be saved, later skipped by runtime preflight, and still appear as a successful command.mcp update."
    suggested_action: "Validate URL scheme and host before persisting HTTP-style MCP servers."
  - id: ISSUE-004
    dimension: observability
    severity: low
    status: open
    summary: "Transport error text omits streamable-http variants."
    evidence: "The allowed set includes streamable-http and streamable_http, but the ValueError message lists only stdio, sse, and http."
    suggested_action: "Align error text and tests with the full supported transport set."
  - id: ISSUE-005
    dimension: test_coverage
    severity: low
    status: open
    summary: "Normalizer boundary and field-preservation behavior lacks direct tests."
    evidence: "Existing command.mcp tests cover happy paths but not invalid transport, non-bool enabled, invalid URL, server_id, or timeout_s preservation."
    suggested_action: "Add narrow unit tests for add/update normalization and command-layer bad-request responses."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._normalize_mcp_payload`

## Actual Role

Merges MCP add/update params with an optional current entry, validates required name/transport-specific text, then rebuilds a whitelisted `mcp.servers` payload. It is pure locally, but callers replace the persisted entry with this output, so omitted or weakly coerced fields change runtime behavior.

## Key Signals

- Input: Raw command params and optional current MCP server config.
- Output: Normalized payload for `mcp.servers`, or `ValueError` for missing required fields.
- Main side effects: None inside the method.
- Main risk: Weak boolean/timeout/URL validation and whitelist rebuilding can persist ineffective values or delete supported fields.
- Related tests: command.mcp list/add/update/minimal-flow tests cover happy paths indirectly.

## Detail Index

- Detail docs pending.
