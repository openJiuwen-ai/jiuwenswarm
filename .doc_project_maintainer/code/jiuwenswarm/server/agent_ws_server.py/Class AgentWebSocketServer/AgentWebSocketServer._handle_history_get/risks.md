---
symbol: AgentWebSocketServer._handle_history_get
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_history_get audit evidence

## ISSUE-001: Unchecked session_id reaches creating filesystem helpers.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Lines 3010-3013 forward params.session_id to get_conversation_history. That helper only strips/non-empty-checks it; history_exists -> get_read_history_path -> _session_dir(create=True) joins it below the sessions root and calls mkdir before file existence checks, with no safe-name or resolved-containment validation.
- Suggested action: Validate a single safe session ID, enforce resolved-root containment, and use create=False for all read paths.

## ISSUE-002: Each page load scans the whole history.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: get_conversation_history lines 6140-6165 synchronously reads/parses the entire JSON/JSONL file, filters every record, materializes a reversed copy, and only then slices one 20-record page. Repeated page requests repeat all work on the event loop.
- Suggested action: Consider reverse JSONL/cursor paging or cached pagination metadata for large sessions.

## ISSUE-003: No direct unary history handler tests were found.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: test_history_payload_limits.py directly covers get_conversation_history sanitization for one large record, while Gateway/E2A tests cover adjacent routing. No test invokes _handle_history_get success/error branches or asserts send_lock, wire identity, metadata, malformed params, or path safety.
- Suggested action: Add async handler tests with fake ws, patched history helper, invalid params, and path-safety cases.

## ISSUE-004: Failure responses are ambiguous and uncoded.

- Dimension: `error_handling`
- Severity: `low`
- Status: `open`
- Evidence: Lines 3014-3020 map invalid type/range, out-of-range page, missing history, non-list data, and caught load failure to the same code-less message. history_exists runs outside get_conversation_history's try, so mkdir/stat permission failures escape to _handle_message's generic error path.
- Suggested action: Return stable BAD_REQUEST/NOT_FOUND/HISTORY_IO codes and keep filesystem probing inside the local error boundary.
