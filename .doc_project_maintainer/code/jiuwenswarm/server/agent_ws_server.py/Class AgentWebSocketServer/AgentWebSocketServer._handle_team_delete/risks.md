---
symbol: AgentWebSocketServer._handle_team_delete
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_team_delete audit evidence

## ISSUE-001: Runner.delete_agent_team false result is ignored.

- Dimension: `implementation_soundness`
- Severity: `medium`
- Status: `open`
- Evidence: Current source awaits Runner.delete_agent_team(..., force=True) without assigning or checking its boolean result, then proceeds to local directory deletion and a deleted=true response.
- Suggested action: Check the returned value and return a stable failure response before deleting local session directories when persistent team deletion fails.

## ISSUE-002: Local session directory deletion failure is masked as successful team deletion.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: Current local cleanup catches each shutil.rmtree exception, logs and continues, but the final payload still returns deleted=true and the complete session_ids list without failed-session detail.
- Suggested action: Track failed local removals and return a partial/failure response, or retry/rollback according to a documented cleanup contract.

## ISSUE-003: Missing direct tests for partial and lower-level failure paths.

- Dimension: `test_coverage`
- Severity: `low`
- Status: `open`
- Evidence: test_agentserver_acp has direct success, checkpointer-unavailable, non-team-mode, and missing-team-name cases. No located direct case covers NOT_FOUND, Runner false/exception, runtime-stop failure, rmtree failure, or partial cleanup reporting.
- Suggested action: Add tests for no matching sessions, Runner false/exception, and rmtree failure so destructive partial-cleanup behavior is pinned.
