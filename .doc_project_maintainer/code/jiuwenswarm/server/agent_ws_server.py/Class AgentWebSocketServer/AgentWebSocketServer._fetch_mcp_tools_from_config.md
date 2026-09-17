---
symbol: AgentWebSocketServer._fetch_mcp_tools_from_config
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_fetch_mcp_tools_from_config(entry: dict[str, Any]) -> list[dict[str, Any]]"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: implicit
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: isolated
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
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
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Temporary MCP lifecycle is not bounded by this method."
    evidence: "connect, list_tools, and disconnect are awaited directly; the adjacent _pre_check_mcp_server instead bounds connect at 15s and disconnect at 5s. HTTP list_tools is bounded only if a process-global patch was applied, while stdio remains uncovered."
    suggested_action: "Bound every phase with cancellation-safe transport-aware timeouts and guarantee cleanup cannot hang."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: medium
    status: open
    summary: "MCP config construction is duplicated and diverges from the shared builder."
    evidence: "The inline McpServerConfig builder omits timeout_s and explicit server_id supported by common.mcp_config.build_mcp_server_config. Dropping timeout_s also prevents the global HTTP timeout patch from honoring the entry's configured limit."
    suggested_action: "Delegate config conversion to build_mcp_server_config and keep one transport/config contract."
  - id: ISSUE-003
    dimension: output_contract
    severity: medium
    status: open
    summary: "An empty result conflates failure with a real zero-tool catalog."
    evidence: "Invalid entries and connect=false return []; show/list_tools callers also catch exceptions and expose tool_count=0 or ok=True with tools=[]."
    suggested_action: "Return structured status or surface a degraded/error result separately from an empty catalog."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "The temporary discovery path has no direct tests."
    evidence: "command.mcp tests cover list/add/update/remove/enable/minimal flows, but not this helper, show/list_tools fallback, timeouts, transport fields, or failure responses."
    suggested_action: "Use fake ToolMgr clients to cover success, invalid config, connect false, timeout/error, timeout_s propagation, cleanup, and both callers."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._fetch_mcp_tools_from_config`

## Actual Role

Builds a temporary MCP client from one config entry, connects, converts tool cards to wire dictionaries, and disconnects. `/mcp show` and `list_tools` use it when cached `ToolMgr` data is absent.

## Key Signals

- Input: One MCP config entry; callers decide whether it is enabled.
- Output: Tool dictionaries, or `[]` for invalid input, failed connect, or no tools.
- Side effects: Opens and closes a temporary subprocess/network client; logs validation and cleanup failures.
- Main risk: Unbounded phases plus lossy config conversion can hang or silently degrade discovery.
- Tests/flows: No direct or fallback test; project docs identify MCP discovery as pending coverage, with no dedicated MCP flow document.

## Detail Index

- Detail docs pending.
