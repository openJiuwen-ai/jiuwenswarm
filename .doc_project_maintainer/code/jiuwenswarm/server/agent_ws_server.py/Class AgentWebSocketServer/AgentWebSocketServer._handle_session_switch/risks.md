---
symbol: AgentWebSocketServer._handle_session_switch
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_switch audit evidence

## ISSUE-001: The handler trusts client-supplied team mode and target identity.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 2403-2429 accept params.session_id/request.session_id plus caller-declared team mode without checking session directory or persisted mode/team_name. In distributed mode TeamManager treats every active/pending id other than that unchecked string as stale and stops it, so a nonexistent target can stop all current channel runtimes.
- Suggested action: Verify that the target exists and is a team session before invoking distributed cleanup.

## ISSUE-002: Delegated switch failures fall through to the outer generic error path.

- Dimension: `error_handling`
- Severity: `low`
- Status: `open`
- Evidence: The await at lines 2427-2432 has no local exception mapping; _handle_message can only return the generic request error payload rather than a session-switch-specific code.
- Suggested action: Catch delegated failures and return a switch-specific error code.

## ISSUE-003: Validation and failure branches lack direct tests.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: test_agentserver_acp.py covers team success and non-team rejection; TeamManager tests cover distributed stale cleanup and local no-op. Missing id, nonexistent/wrong-mode target, delegated exception, metadata passthrough, and truthful success semantics are not covered.
- Suggested action: Test those branches and metadata passthrough.

## ISSUE-004: The success response overstates the performed operation.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 2433-2441 always return mode='team' and switched=true. prepare_session_switch is a local-runtime no-op and in distributed mode only stops other sessions; neither layer loads or activates the target, and team.plan/code.team input variants are collapsed to team.
- Suggested action: Report a preparation result with the requested canonical mode, or activate/verify the target before claiming switched=true.
