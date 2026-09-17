---
symbol: AgentWebSocketServer._handle_agents_set_enabled
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_agents_set_enabled audit evidence

## ISSUE-001: Config mutation is not rolled back when runtime reload fails.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: Current sequence calls upsert_subagent_in_config before reload_agents_config. A reload exception is caught locally, leaving durable enabled state changed while returning ok=true with applied=false and no rollback.
- Suggested action: Apply atomically or restore the previous config value when reload fails.

## ISSUE-002: Workspace validation leads to a global enable state and global reload.

- Dimension: `dependency_coupling`
- Severity: `high`
- Status: `open`
- Evidence: workspace_dir scopes AgentConfigService.get_agent validation, but upsert_subagent_in_config writes shared config.yaml and reload_agents_config(get_config(), None) reconciles all cached runtimes rather than the request channel/project.
- Suggested action: Define global versus workspace ownership and reload only affected runtimes.

## ISSUE-003: Concurrent agent/config commands can lose YAML updates.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: AgentServer request tasks run concurrently, while config upsert performs a shared YAML read-modify-write without a handler mutation lock; send_lock serializes only this connection's response writes.
- Suggested action: Serialize shared config mutations with one lock and use atomic file replacement.

## ISSUE-004: The primary TUI reports success even when applied is false.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: On reload failure the method returns top-level ok=true plus applied=false/reload_error. The primary UI success path keys on request success and can announce enabled/disabled without treating applied=false as failure.
- Suggested action: Return transport failure or require clients to surface partial application explicitly.

## ISSUE-005: Enable/disable orchestration lacks direct handler coverage.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Service/config helper tests cover agent lookup and YAML upsert behavior, but none invokes _handle_agents_set_enabled or checks validation, workspace/global scope, reload failure, concurrency, payloads, locking, or UI presentation.
- Suggested action: Add handler tests with temp config, fake manager, concurrent changes, and TUI assertions.
