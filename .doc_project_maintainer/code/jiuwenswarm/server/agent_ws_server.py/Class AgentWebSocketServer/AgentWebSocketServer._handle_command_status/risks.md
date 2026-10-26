---
symbol: AgentWebSocketServer._handle_command_status
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_status audit evidence

## ISSUE-001: usage.models_used reports session modes rather than models.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: Current usage branch groups each metadata record's mode into model_counts and emits those keys as models_used[].name; session mode (for example agent/team) is not model identity.
- Suggested action: Aggregate model identity or rename the field to modes_used.

## ISSUE-002: Usage aggregates are silently truncated to the newest 500 sessions.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: Current code calls get_all_sessions_metadata(limit=500, offset=0), computes messages/models/days/duration only from that page, but emits the separate full total as sessions_total without a sampled/truncated marker.
- Suggested action: Aggregate all pages or expose sample scope/truncation explicitly.

## ISSUE-003: The overview read path mutates memory cache and performs synchronous filesystem discovery.

- Dimension: `side_effects`
- Severity: `medium`
- Status: `open`
- Evidence: Every overview calls clear_project_memory_cache(workspace_dir), then synchronously discover_and_load_memory_files and get_large_memory_files on the event loop before constructing the response.
- Suggested action: Use cache-aware diagnostics off the event loop; do not clear shared cache for status.

## ISSUE-004: Memory diagnostic failure is indistinguishable from a clean result.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: The overview catches all memory diagnostic exceptions, logs a warning, assigns memory_warnings=[], and returns ok=true without availability/degraded/error metadata.
- Suggested action: Return diagnostic availability/error metadata separately from an empty warning list.

## ISSUE-005: No direct status-handler tests were found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Repository search finds no direct _handle_command_status invocation or payload assertion; the only COMMAND_STATUS test references are Gateway stream-cancel/timeout routing with a fake client.
- Suggested action: Test all actions, >500 sessions, model aggregation, memory failures, encoding, and locking.
