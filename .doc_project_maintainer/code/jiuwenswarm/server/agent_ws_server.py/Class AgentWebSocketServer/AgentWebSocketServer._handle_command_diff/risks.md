---
symbol: AgentWebSocketServer._handle_command_diff
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_diff audit evidence

## ISSUE-001: Resolved project_dir is trusted before filesystem and git-diff reads.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `open`
- Evidence: resolve_request_project_dir accepts params/metadata project_dir or cwd and params.trusted_dirs[0]; line 3795 passes the result directly to DiffService, which reads that directory's .agent_history and runs fixed git commands with it as cwd.
- Suggested action: Validate canonical project_dir against trusted/session-bound directories, or derive it from server-side session metadata when available.

## ISSUE-002: Direct coverage exercises only the empty default success path.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: test_handle_command_diff_returns_summary_payload exercises the real singleton only for {type:list, turns:[]} with default inputs; no direct test covers fake-service turns, gitDiff inclusion, explicit project_dir/cwd routing, errors, dispatch, or frontend flow.
- Suggested action: Add handler tests with a fake DiffService for turns, gitDiff, and error paths, plus a gateway/TUI forwarding smoke test.

## ISSUE-003: session_id controls unchecked history and metadata paths.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: request.session_id reaches load_history_records and get_agent_sessions_dir() / session_id / metadata.json without canonical-ID or containment checks. The history path helpers call _session_dir with create=True even for reads, so traversal-shaped values can also create directories.
- Suggested action: Validate a canonical ID, enforce sessions-root containment, and make reads non-creating.

## ISSUE-004: Turn-diff computation and payload construction are unbounded across session history.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: get_turn_diffs reads full history and matching file-operation logs and returns every changed turn. Per-file hunks are capped and send_wire_payload has a final byte-budget fallback, but both full computation and initial serialization occur before that fallback.
- Suggested action: Add cursor/limit or a total turn-diff budget before computation/payload construction, and return explicit truncation metadata instead of relying on the oversized-response fallback.
