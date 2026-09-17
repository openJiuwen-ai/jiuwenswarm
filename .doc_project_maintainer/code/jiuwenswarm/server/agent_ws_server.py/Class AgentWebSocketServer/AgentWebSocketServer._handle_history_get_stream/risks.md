---
symbol: AgentWebSocketServer._handle_history_get_stream
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_history_get_stream audit evidence

## ISSUE-001: Unvalidated session_id flows into session history path construction.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: params.session_id is passed unchanged to get_conversation_history; history_exists/get_read_history_path compose root/session_id through _session_dir(create=True), so a read can create an unchecked absolute or traversed directory.
- Suggested action: Require a safe single-name ID, enforce resolved-root containment, make read paths non-creating, and add traversal/absolute-path tests.

## ISSUE-002: Invalid stream history emits chat.error instead of a history-scoped terminal frame.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: When get_conversation_history returns None, the method sends only an is_complete chat.error chunk. Normal pages terminate with history.message status=done, so the history-specific terminal contract differs on validation/not-found.
- Suggested action: Emit a history-scoped terminal error/done frame or explicitly resolve pending page waiters on chat.error.

## ISSUE-003: Each page synchronously processes the full history on the event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: The handler synchronously calls get_conversation_history on the event loop; that helper reads/parses the complete file, filters all records, reverses them, and only then slices one page.
- Suggested action: Move reads off-loop and use reverse JSONL/cursor paging or cached pagination metadata for large sessions.

## ISSUE-004: No direct stream-handler test evidence was found.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Tests cover get_conversation_history payload limits, gateway routing, and generic history.message codec roundtrip, but repository search finds no direct call to _handle_history_get_stream.
- Suggested action: Add fake-websocket tests for valid/invalid pages, sequence numbers, done publication, traversal, and send failure.

## ISSUE-005: An oversized record terminates the page without its history done frame.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: If send_wire_payload replaces a record chunk and returns false, the method logs and returns immediately; it never emits the final history.message status=done chunk for that page.
- Suggested action: Define the replacement as an explicit terminal history error or emit a bounded history-scoped done/error frame, and test consumer finalization.
