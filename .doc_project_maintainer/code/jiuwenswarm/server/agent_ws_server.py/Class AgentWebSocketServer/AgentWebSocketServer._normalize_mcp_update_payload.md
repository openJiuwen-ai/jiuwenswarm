---
symbol: AgentWebSocketServer._normalize_mcp_update_payload
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_normalize_mcp_update_payload(self, params: dict[str, Any]) -> dict[str, Any]"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: safe
  input_contract: clear
  output_contract: clear
  side_effects: explicit
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
  audited_symbol_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: low
    status: open
    summary: "Update not-found behavior lacks a dedicated regression test."
    evidence: "Lines 4183-4189 raise KeyError when lookup returns None. test_agentserver_cli_commands.py:535-576 covers update success, while MCP_NOT_FOUND is asserted only for enable at lines 475-496."
    suggested_action: "Add action=update not-found coverage asserting MCP_NOT_FOUND and no upsert or reload call."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._normalize_mcp_update_payload`

## Actual Role

Ensures a `/mcp update` request targets an existing server before normalization. It validates a non-empty `name`, fetches the current config, raises `KeyError` when absent so update cannot accidentally create a new server, and delegates merge/validation to `_normalize_mcp_payload`.

## Key Signals

- Input: Raw update params with a required server name.
- Output: Normalized payload merged with the existing config, or `KeyError`/`ValueError`.
- Main side effects: Reads current MCP config through `get_mcp_server_config`.
- Main risk: The not-found contract is important but not directly tested for update.
- Related tests: MCP update happy path and enable-not-found response tests.

## Detail Index

- Detail docs pending.
