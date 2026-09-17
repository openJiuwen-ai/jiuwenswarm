---
symbol: AgentWebSocketServer._handle_command_recap
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_recap audit evidence

## ISSUE-001: No direct handler or adapter generate_recap status-path tests cover command.recap.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: test_btw_command.py covers recap prompt construction and the shared _call_model_for_recap helper, but static search found no test that calls _handle_command_recap, COMMAND_RECAP dispatch, or generate_recap's ok/no_turn/failed branches.
- Suggested action: Add handler tests for success, no-turn, missing agent, exception, default session, auto_harness, and project_dir routing, plus adapter status-path tests.

## ISSUE-002: Success and adapter-level failed statuses are not logged at the handler boundary.

- Dimension: `observability`
- Severity: `low`
- Status: `open`
- Evidence: Lines 3690-3699 delegate and log only raised exceptions; normal ok, no_turn, and returned failed statuses are not logged at the command boundary.
- Suggested action: Log recap result status after generate_recap without recording summary content.

## ISSUE-003: Raised business failures have no stable machine-readable error code.

- Dimension: `error_handling`
- Severity: `low`
- Status: `open`
- Evidence: Lines 3698-3707 collapse lookup, initialization, and unexpected recap exceptions into ok=false/status=failed with the raw exception string. By contrast, returned status=failed is an intentional application outcome consumed by the frontend switch while transport remains ok=true.
- Suggested action: Map expected lookup and recap failures to stable error codes while preserving the documented application-level status contract.
