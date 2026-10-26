---
symbol: AgentWebSocketServer._handle_config_cache_clear
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_config_cache_clear audit evidence

## ISSUE-001: The generic cache-clear RPC invalidates only a narrow memory-config dictionary.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: Current handler only calls memory.config.clear_config_cache, which assigns that module's _config_cache=None. It does not reload the value immediately, reconcile agents, or rebuild existing memory managers like the newer AGENT_RELOAD_CONFIG path.
- Suggested action: Remove the endpoint or route it through scoped agent reload and report actual refreshed state.

## ISSUE-002: The request enum and handler are orphaned from current Gateway callers.

- Dimension: `dependency_coupling`
- Severity: `medium`
- Status: `open`
- Evidence: Current production Web/TUI helpers named _clear_agent_config_cache send AGENT_RELOAD_CONFIG. Repository search finds CONFIG_CACHE_CLEAR only in schema and AgentServer dispatch/handler, with no production sender.
- Suggested action: Deprecate and remove the stale method after compatibility review, or document a real caller and align naming/semantics across the wire.

## ISSUE-003: cleared=true overstates configuration application.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: The response always reports cleared=true after assigning None, although active components retain their loaded configuration and the payload exposes no invalidated scope, revision, reload, or affected-runtime status.
- Suggested action: Return explicit invalidated/reloaded scopes and configuration revision, with degraded/error state when active components were not refreshed.

## ISSUE-004: No cache-clear RPC or lifecycle test exists.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: No test references config.cache_clear, CONFIG_CACHE_CLEAR, _handle_config_cache_clear, or memory.config.clear_config_cache. Existing Web helper tests exercise the replacement AGENT_RELOAD_CONFIG path.
- Suggested action: Add wire-level compatibility tests or delete the endpoint; cover cache invalidation, active memory-manager refresh, concurrent reads, error responses, and caller method selection.

## ISSUE-005: Cache invalidation can race with an in-progress reload.

- Dimension: `state_mutation`
- Severity: `medium`
- Status: `open`
- Evidence: clear_config_cache and memory.config._load_config share _config_cache without a lock or generation. If clear runs while _load_config reads YAML, that older load can assign _config_cache after the clear and negate invalidation.
- Suggested action: Serialize cache reads/clears or use a revision token so pre-clear loads cannot publish afterward.
