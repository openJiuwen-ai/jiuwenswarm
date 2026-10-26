---
symbol: AgentWebSocketServer._handle_agents_tools_list
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_agents_tools_list audit evidence

## ISSUE-001: The endpoint reports a static catalog as currently available tools.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: Current list_available_tools combines OpenJiuwen's static display-name dictionary with local TOOL_GROUPS and descriptions; it never inspects the AbilityManager/ToolCards actually registered for the requesting runtime.
- Suggested action: Build from effective runtime capabilities, then add stable display metadata.

## ISSUE-002: A server RPC depends on a private CLI UI symbol.

- Dimension: `dependency_coupling`
- Severity: `high`
- Status: `open`
- Evidence: The service imports private openjiuwen.harness.cli.ui.tool_display._TOOL_DISPLAY_NAMES from a server RPC path, coupling catalog availability to CLI UI internals and their import chain.
- Suggested action: Move tool identity/display metadata to one public, UI-independent shared module.

## ISSUE-003: Tool identity crosses the UI/runtime boundary through two unsynchronized mappings.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: The catalog returns both display and inferred internal names from one mapping, while runtime filtering translates persisted display selections through a separate code_agent_rail mirror; mapping drift can silently omit selected ToolCards.
- Suggested action: Persist one canonical tool ID and keep display labels presentation-only.

## ISSUE-004: No service or handler contract tests were found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: test_code_agent_rail asserts several TOOL_GROUPS/DISALLOWED constants, but no located test calls list_available_tools or _handle_agents_tools_list, compares runtime abilities, simulates map drift/import failure, or verifies a UI round trip.
- Suggested action: Add catalog/handler tests and a canonical-ID TUI-to-AgentTool round trip.

## ISSUE-005: workspace_dir is accepted but has no effect on the result.

- Dimension: `input_contract`
- Severity: `low`
- Status: `open`
- Evidence: The handler reads workspace_dir and constructs AgentConfigService(workspace_dir), but list_available_tools is static and never reads service workspace state.
- Suggested action: Remove the unused selector from this contract or make capability discovery explicitly runtime/workspace scoped.
