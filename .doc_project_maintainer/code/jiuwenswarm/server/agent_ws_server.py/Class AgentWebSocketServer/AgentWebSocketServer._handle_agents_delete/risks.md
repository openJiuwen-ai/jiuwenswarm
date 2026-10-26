---
symbol: AgentWebSocketServer._handle_agents_delete
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_agents_delete audit evidence

## ISSUE-001: The request selects an arbitrary workspace and ambiguous definition source.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, params.workspace_dir is passed directly to AgentConfigService and name is neither type-checked nor normalized. The service can select the highest-priority local/user/project definition by name and unlink it without this handler requiring an authorized project identity, explicit source, or expected revision.
- Suggested action: Resolve an authorized project identity and require expected source/revision before unlinking.

## ISSUE-002: File deletion, global config mutation, and runtime reload are not transactional.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, service.delete_agent unlinks first, remove_subagent_from_config edits global config next, and reload_agents_config runs last. Config/reload failure is swallowed into applied=false with no rollback or retry, so the definition can be gone while global config or live runtimes remain stale.
- Suggested action: Use a recoverable transaction: stage the file, atomically update config, reload, then commit or restore/retry.

## ISSUE-003: A missing definition still mutates config and returns top-level success.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, service.delete_agent can return false for an unknown name, but the handler still removes that name from global subagent config and reloads. It then always emits AgentResponse(ok=true), nesting deletion failure as payload.ok=false and potentially reporting applied=true for orphan-config cleanup.
- Suggested action: Validate first and distinguish not-found, deleted, orphan cleanup, partial failure, and success at top level.

## ISSUE-004: Helper tests exist, but the delete RPC lifecycle is untested.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Service/helper tests cover basic deletion and config round trips. No direct RPC/handler test covers workspace/source selection, malformed name, not-found config mutation, partial failure, concurrency, reload outcome, or nested wire semantics.
- Suggested action: Add handler/Gateway contracts, failure/concurrency cases, and an agent-configuration flow.

## ISSUE-005: Synchronous deletion and config reads/writes run on the WebSocket event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, AgentConfigService construction/deletion, remove_subagent_from_config, and get_config execute synchronously inside the async handler before the awaited global reload and response send. Filesystem or config-lock latency blocks unrelated AgentServer work.
- Suggested action: Move persistent agent/config mutations behind an async serialized service or worker thread, preserving atomic ordering.
