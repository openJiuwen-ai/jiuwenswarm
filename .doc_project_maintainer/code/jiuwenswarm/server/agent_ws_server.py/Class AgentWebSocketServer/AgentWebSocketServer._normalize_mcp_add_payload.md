---
symbol: AgentWebSocketServer._normalize_mcp_add_payload
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_normalize_mcp_add_payload(self, params: dict[str, Any]) -> dict[str, Any]"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: weak
  output_contract: weak
  side_effects: none
  error_handling: partial
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
    dimension: input_contract
    severity: medium
    status: open
    summary: "Non-boolean enabled values are accepted by Python truthiness."
    evidence: "The delegated normalizer uses bool(value), so enabled='false' becomes True; the add caller then uses and persists that value."
    suggested_action: "Require a boolean or centrally parse documented true/false forms."
  - id: ISSUE-002
    dimension: output_contract
    severity: medium
    status: open
    summary: "Add normalization drops or weakly validates supported runtime fields."
    evidence: "The delegated whitelist drops server_id and stdio timeout_s; HTTP timeout_s accepts bool, zero, negative, and fractional values even though the shared runtime builder requires a positive numeric limit."
    suggested_action: "Reuse one MCP schema, preserve supported fields, and require a positive non-bool timeout."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "HTTP/SSE URLs are accepted when merely non-empty."
    evidence: "The add caller only pre-checks selected stdio file arguments, so a malformed HTTP URL can be persisted and reported as added before runtime registration rejects or skips it."
    suggested_action: "Validate URL scheme and host before persistence."
  - id: ISSUE-004
    dimension: error_handling
    severity: low
    status: open
    summary: "Invalid transport guidance is incomplete."
    evidence: "The delegated normalizer accepts streamable-http variants but reports only stdio, sse, and http in the error text."
    suggested_action: "Report the complete accepted transport set."
  - id: ISSUE-005
    dimension: test_coverage
    severity: low
    status: open
    summary: "Only indirect add happy paths exercise this wrapper."
    evidence: "Tests cover stdio/SSE add and reload, but not direct normalization, non-bool enabled, malformed URL, invalid transport, server_id, timeout_s, or MCP_BAD_REQUEST responses."
    suggested_action: "Add direct boundary tables plus command-layer bad-request assertions."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._normalize_mcp_add_payload`

## Actual Role

Acts as the `/mcp add` normalization entry point. It adds no behavior beyond delegating to `_normalize_mcp_payload`; the caller then pre-checks selected stdio inputs, upserts the result, reloads agents, and maps validation exceptions to `MCP_BAD_REQUEST`.

## Key Signals

- Input: Raw add params from `command.mcp`.
- Output: Normalized MCP server payload.
- Main side effects: None.
- Main risk: Add inherits weak boolean/timeout/URL validation and a whitelist that loses supported fields.
- Related tests/flows: Add command tests cover happy paths indirectly; no direct boundary test or dedicated MCP flow document was found.

## Detail Index

- Detail docs pending.
