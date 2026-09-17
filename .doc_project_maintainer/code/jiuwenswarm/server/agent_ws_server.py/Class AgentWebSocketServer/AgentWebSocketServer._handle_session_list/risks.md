---
symbol: AgentWebSocketServer._handle_session_list
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_list audit evidence

## ISSUE-001: Session list handler bypasses shared helper semantics.

- Dimension: `implementation_soundness`
- Severity: `medium`
- Status: `open`
- Evidence: Current HEAD 39feee89 still scans get_agent_sessions_dir() directly. In contrast, get_all_sessions_metadata excludes heartbeat_* directories, supplies created_at/user_id/mode fallbacks, sorts by last_message_at, and returns total; this handler includes heartbeat directories and emits a smaller fallback shape ordered by directory mtime.
- Suggested action: Share one cache-bust-capable listing helper with consistent exclusion, fallback, ordering, and totals.

## ISSUE-002: The TUI filtering contract requires an unbounded full-list transfer.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: The handler ignores request.params, including the Gateway/TUI limit, stats and cache-bust-reads every directory, and returns all results. TUI then filters by channel/project/current session before applying its limit, so work and payload grow without a cap and naive early pagination would change visible results.
- Suggested action: Define equivalent server filters before pagination, or document and cap the full-list contract.

## ISSUE-003: Directory scan failure is reported as ok=true.

- Dimension: `error_handling`
- Severity: `low`
- Status: `open`
- Evidence: One try covers exists(), iterdir(), sorting stats, per-entry is_dir/stat, and metadata reads. Any exception logs one warning, aborts the remaining scan, then still sends ok=true with the partial list and no warning field.
- Suggested action: Return ok=false for total failure, or include a partial-result warning.

## ISSUE-004: No direct handler coverage found.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Current tests cover get_all_sessions_metadata sorting/pagination/fallback/heartbeat exclusion and Gateway session.list timeout/registration, but no located AgentServer test invokes _handle_session_list or asserts its scan, partial-error, encoding, metadata, bounded-send, or lock behavior.
- Suggested action: Add handler tests for filtering semantics, fallback metadata, scan errors, encoding, and locked send.
