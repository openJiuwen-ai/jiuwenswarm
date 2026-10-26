---
symbol: AgentWebSocketServer._handle_agents_tools_list
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_agents_tools_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: partial
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:34Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:ae5ee6e102d8327447a18288aa89e889370b7ea5dcd972d53d0e35be0675bb42
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: high
    status: open
    summary: "The endpoint reports a static catalog as currently available tools."
    evidence: "Current list_available_tools combines OpenJiuwen's static display-name dictionary with local TOOL_GROUPS. See AgentWebSocketServer._handle_agents_tools_list/risks.md#issue-001."
    suggested_action: "Build from effective runtime capabilities, then add stable display metadata."
  - id: ISSUE-002
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "A server RPC depends on a private CLI UI symbol."
    evidence: "The service imports private openjiuwen.harness.cli.ui.tool_display._TOOL_DISPLAY_NAMES from a server RPC. See AgentWebSocketServer._handle_agents_tools_list/risks.md#issue-002."
    suggested_action: "Move tool identity/display metadata to one public, UI-independent shared module."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Tool identity crosses the UI/runtime boundary through two unsynchronized mappings."
    evidence: "The catalog returns both display and inferred internal names from one mapping, while runtime filtering. See AgentWebSocketServer._handle_agents_tools_list/risks.md#issue-003."
    suggested_action: "Persist one canonical tool ID and keep display labels presentation-only."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No service or handler contract tests were found."
    evidence: "test_code_agent_rail asserts several TOOL_GROUPS/DISALLOWED constants, but no located test calls. See AgentWebSocketServer._handle_agents_tools_list/risks.md#issue-004."
    suggested_action: "Add catalog/handler tests and a canonical-ID TUI-to-AgentTool round trip."
  - id: ISSUE-005
    dimension: input_contract
    severity: low
    status: open
    summary: "workspace_dir is accepted but has no effect on the result."
    evidence: "The handler reads workspace_dir and constructs AgentConfigService(workspace_dir), but. See AgentWebSocketServer._handle_agents_tools_list/risks.md#issue-005."
    suggested_action: "Remove the unused selector from this contract or make capability discovery explicitly runtime/workspace scoped."
---

# AgentWebSocketServer._handle_agents_tools_list

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_agents_tools_list/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_agents_tools_list/risks.md)
