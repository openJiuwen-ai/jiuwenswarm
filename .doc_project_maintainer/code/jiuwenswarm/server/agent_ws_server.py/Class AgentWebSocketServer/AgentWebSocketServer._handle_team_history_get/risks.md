---
symbol: AgentWebSocketServer._handle_team_history_get
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_team_history_get audit evidence

## ISSUE-001: Pagination happens after full history load and full-record sanitization.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: Current handler awaits read_team_history_records/read_member_history_records for the full session, sanitizes every dict result into a second list, then computes total/coerces cursor and finally calls _select_history_record_page.
- Suggested action: Move pagination closer to storage or slice by cursor before expensive sanitization while preserving total and has_more semantics.

## ISSUE-002: Read failures are indistinguishable from empty history to clients.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: The broad read try/except catches team and member reader failures, logs one warning, assigns records=[], and continues to an ok=true payload with total=0 and no warning/error field.
- Suggested action: Return ok=false or include an explicit non-fatal warning/error field for storage failures, with direct tests for that path.

## ISSUE-003: Unchecked session_id reaches creating filesystem helpers.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: session_id is only type/blank checked and stripped before reaching get_read_history_path. That helper calls _history_jsonl_file/_history_file, whose _session_dir defaults create=True and composes sessions_root / session_id without containment.
- Suggested action: Validate a safe ID, enforce sessions-root containment, and make reads non-creating.

## ISSUE-004: Failure and boundary paths lack direct coverage.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: test_history_payload_limits directly covers paging, cursor continuation, bounded/truncated records, and placeholder output; no located direct case covers missing id, reader exception, member filtering, coercion edges, creating reads, or traversal.
- Suggested action: Add handler tests for validation, read failure, member scope, and hostile paths.
