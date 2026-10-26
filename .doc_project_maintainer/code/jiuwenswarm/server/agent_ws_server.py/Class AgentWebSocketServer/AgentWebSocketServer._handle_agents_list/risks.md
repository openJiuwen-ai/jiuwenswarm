---
symbol: AgentWebSocketServer._handle_agents_list
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_agents_list audit evidence

## ISSUE-001: Workspace identity is incompatible with normal clients and unrestricted when supplied.

- Dimension: `input_contract`
- Severity: `high`
- Status: `open`
- Evidence: Current handler reads only params.workspace_dir and ignores request project_dir/cwd identity. AgentConfigService falls back to AgentServer Path.cwd() when absent; when supplied, workspace_dir becomes Path(workspace_dir) without authorization, canonical project matching, or trusted-root validation.
- Suggested action: Resolve a canonical authorized project identity shared with clients; reject arbitrary or ambiguous workspace roots.

## ISSUE-002: The list endpoint exposes complete agent definitions.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: Current response applies dataclass_asdict to every active and shadowed AgentDefinition, including prompt, file_path, tools, skills, model, and all other fields; agents.get already provides a separate detail endpoint.
- Suggested action: Return a bounded summary DTO and gate full prompt/path disclosure behind an authorized detail endpoint.

## ISSUE-003: Synchronous full-content discovery is unbounded on the WebSocket event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: Before its first await, AgentConfigService.list_agents scans local/user/project directories, reads/parses all definitions and global config, then the handler materializes full dataclass dictionaries without pagination or item/count cap.
- Suggested action: Move file discovery off-loop, cache by file metadata, and paginate/cap list summaries.

## ISSUE-004: Handler and cross-boundary list contracts lack direct coverage.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: test_agent_config_service.py covers service-level builtins, project merge, precedence, and sorting, but no test invokes _handle_agents_list or covers workspace identity, arbitrary paths, prompt redaction, large output, config-state degradation, wire errors, or TUI request parity.
- Suggested action: Add temporary-directory service tests and TUI/Gateway-to-AgentServer contract tests.
