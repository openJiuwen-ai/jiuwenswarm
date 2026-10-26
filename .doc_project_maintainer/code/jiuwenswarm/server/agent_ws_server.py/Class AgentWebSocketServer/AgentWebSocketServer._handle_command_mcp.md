---
symbol: AgentWebSocketServer._handle_command_mcp
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_mcp(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: overloaded
  length: excessive
  complexity: high
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: implicit
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: high
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:44Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:2392b3054e91d770a17d4e9afdf1dea082f9b01034861ad55a9c6411b4117e3c
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Add/update normalization can drop runtime-supported fields."
    evidence: "See AgentWebSocketServer._handle_command_mcp/risks.md#issue-001 for full evidence."
    suggested_action: "Define one shared persisted-entry schema with the runtime builder and preserve."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Temporary tool discovery is not explicitly timeout-bounded."
    evidence: "See AgentWebSocketServer._handle_command_mcp/risks.md#issue-002 for full evidence."
    suggested_action: "Use the shared MCP builder, bound connect/list/disconnect individually and."
  - id: ISSUE-003
    dimension: output_contract
    severity: medium
    status: open
    summary: "list_tools can return ok=True with an empty tool list for an unknown server."
    evidence: "See AgentWebSocketServer._handle_command_mcp/risks.md#issue-003 for full evidence."
    suggested_action: "Return MCP_NOT_FOUND when runtime and config both miss the server."
  - id: ISSUE-004
    dimension: performance_risk
    severity: medium
    status: open
    summary: "action=update rewrites config and enters reload even when normalization is unchanged."
    evidence: "See AgentWebSocketServer._handle_command_mcp/risks.md#issue-004 for full evidence."
    suggested_action: "Reuse the add-path equality check before persisting or invoking reload, and."
  - id: ISSUE-005
    dimension: state_mutation
    severity: high
    status: open
    summary: "Persisted config and live runtime can diverge after reload failure."
    evidence: "See AgentWebSocketServer._handle_command_mcp/risks.md#issue-005 for full evidence."
    suggested_action: "Use transactional apply/rollback or a durable pending-reload state, and make."
  - id: ISSUE-006
    dimension: output_contract
    severity: medium
    status: open
    summary: "HTTP/SSE add can report applied=True even if runtime registration is skipped."
    evidence: "See AgentWebSocketServer._handle_command_mcp/risks.md#issue-006 for full evidence."
    suggested_action: "Preflight HTTP/SSE before persistence or propagate per-server runtime."
  - id: ISSUE-007
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Sensitive-field masking misses common MCP credential names."
    evidence: "See AgentWebSocketServer._handle_command_mcp/risks.md#issue-007 for full evidence."
    suggested_action: "Expand credential-key/value detection and add nested env/header tests for."
---

# AgentWebSocketServer._handle_command_mcp

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_mcp/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_mcp/risks.md)
