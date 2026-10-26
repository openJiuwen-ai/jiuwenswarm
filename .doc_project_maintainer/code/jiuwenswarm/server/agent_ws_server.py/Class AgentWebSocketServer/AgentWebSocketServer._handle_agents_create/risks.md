---
symbol: AgentWebSocketServer._handle_agents_create
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_agents_create audit evidence

## ISSUE-001: Creation, enablement, and runtime reload are not transactional.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: Current sequence writes the Markdown file first, then upserts shared config and reloads agents inside one softened try. Upsert failure leaves the file and returns ok=false; reload failure leaves file+config, but returns ok=true with applied=false.
- Suggested action: Commit atomically or roll back and return structured partial failure.

## ISSUE-002: Create silently overwrites an existing custom agent.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: AgentConfigService.create_agent rejects a same-name active builtin but writes name.md without a custom-file existence guard. Service tests pin same-name custom creation as overwrite, and this handler has no replace flag.
- Suggested action: Reject existing custom names by default and require an explicit replace/update operation.

## ISSUE-003: A workspace-scoped request triggers global config and global runtime reload.

- Dimension: `dependency_coupling`
- Severity: `high`
- Status: `open`
- Evidence: workspace_dir selects the definition file root, but upsert_subagent_in_config writes shared config.yaml and reload_agents_config(get_config(), None) reconciles global cached runtimes rather than request.channel_id/project identity.
- Suggested action: Define ownership and reload only affected runtimes.

## ISSUE-004: Unknown request fields are silently discarded and generate is not type-validated.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: After popping workspace_dir/generate, the handler filters params by CreateAgentParams.__dataclass_fields__, silently dropping unknown/misspelled keys. generate defaults true and any truthy non-boolean enables LLM generation.
- Suggested action: Reject extras and type-check controls before side effects.

## ISSUE-005: Handler orchestration and cross-boundary effects lack direct tests.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: AgentConfigService tests cover create/file/overwrite behavior, but no test invokes _handle_agents_create or covers LLM fallback, workspace authorization, config/reload failure, rollback, response flags, or wire send.
- Suggested action: Add async handler tests with fake generation, temp state, failures, and WebSocket assertions.

## ISSUE-006: Unvalidated workspace_dir controls the agent-definition write root.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: The handler passes request workspace_dir directly to AgentConfigService; project/local create locations mkdir and write below that arbitrary host path without canonical project identity or trusted-root authorization.
- Suggested action: Resolve workspace from authenticated request project identity and enforce authorized-root containment before any directory creation or write.
