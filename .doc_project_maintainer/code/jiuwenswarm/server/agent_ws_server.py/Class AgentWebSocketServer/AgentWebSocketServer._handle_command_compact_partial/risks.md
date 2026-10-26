---
symbol: AgentWebSocketServer._handle_command_compact_partial
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_compact_partial audit evidence

## ISSUE-001: turn_index is not validated as a positive 1-based value.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Current handler defaults a missing turn_index to 0, int-coerces without a >=1 check, and passes it through. The adapter indexes user_positions[turn_index - 1], so 0 selects the last user turn and negative values use Python negative indexing or raise.
- Suggested action: Require a present integer turn_index >= 1 before resolving or calling an agent.

## ISSUE-002: The delegated adapter reads only legacy history.json while current session history prefers history.jsonl.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: Current deep adapter compact_partial constructs sessions_dir/session_id/history.json and calls _read_history directly, while session_history defaults writes/reads to history.jsonl unless legacy mode is enabled.
- Suggested action: Use the shared session history resolver/load helpers and add JSONL integration coverage.

## ISSUE-003: session_id controls an unchecked history read path.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Handler defaults request.session_id but performs no id validation; the deep adapter composes sessions_dir / session_id / history.json without rejecting absolute or parent traversal paths or checking containment.
- Suggested action: Validate a canonical session ID and enforce containment under sessions_dir.

## ISSUE-004: No direct handler tests cover compact_partial routing, validation, response, and error behavior.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: test_compact_partial.py covers the separate compact_partial_session service and prompts; repository search finds no direct _handle_command_compact_partial or COMMAND_COMPACT_PARTIAL handler test.
- Suggested action: Add handler tests for success, missing/invalid turn_index, adapter failures, and mode/project forwarding.

## ISSUE-005: Adapter-level failed and no-turn results are reported as top-level success.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: After agent.compact_partial returns, the handler unconditionally builds AgentResponse(ok=True, payload=result_data), although the adapter normally returns status=no_turn or status=failed for missing history, invalid direction, model failure, or empty output.
- Suggested action: Map adapter status to a stable top-level ok/error contract and test each normal failure result.

## ISSUE-006: The broad BaseException handler can swallow process-level exits.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: The method catches BaseException and rethrows only KeyboardInterrupt and asyncio.CancelledError; SystemExit and other non-Exception control-flow failures are logged and converted to an RPC response.
- Suggested action: Catch Exception, with an explicit CancelledError passthrough if required.
