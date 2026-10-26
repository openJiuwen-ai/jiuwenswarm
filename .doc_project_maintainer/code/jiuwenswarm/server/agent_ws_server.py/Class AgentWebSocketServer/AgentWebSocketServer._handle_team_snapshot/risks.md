---
symbol: AgentWebSocketServer._handle_team_snapshot
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_team_snapshot audit evidence

## ISSUE-001: No direct handler or dispatch-route coverage for team.snapshot.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, tests/unit_tests/agentserver/test_monitor_handler.py covers TeamMonitorHandler payload shaping and test_team_helpers.py covers a separate broadcast helper. No test invokes _handle_team_snapshot or verifies TEAM_SNAPSHOT routing, fallback responses, response channel, or send behavior.
- Suggested action: Add async websocket-handler tests for active snapshot, missing/stopped monitor fallback, callee failure fallback, and dispatcher routing.

## ISSUE-002: Different states collapse into the same successful empty response.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, a missing or stopped handler, a None/falsy snapshot, and a handler exception all return ok=true with the identical empty members/tasks/team_id payload. TeamMonitorHandler.get_team_snapshot itself catches any member/team/task query failure and returns None, so even a task-only read failure can erase otherwise available member data from this response.
- Suggested action: Add snapshot_status metadata if clients need diagnostics, or document the refresh-tolerant empty response contract.

## ISSUE-003: Read-style snapshot requests can initialize global team runtime state.

- Dimension: `state_mutation`
- Severity: `low`
- Status: `open`
- Evidence: At HEAD 39feee89, get_team_manager(channel_id) explicitly ignores channel_id and lazily creates the process-wide TeamManager singleton when absent, so this nominally read-only endpoint can initialize global runtime state before returning an empty snapshot.
- Suggested action: Use a non-creating lookup for read-only snapshots, or document singleton initialization as acceptable.
