---
symbol: AgentWebSocketServer._handle_session_rewind_context
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_rewind_context audit evidence

## ISSUE-001: Non-atomic rewind can leave history truncated after context failure.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: Current handler calls synchronous rewind_session first; it truncates history, queues metadata count update, and best-effort truncates file_ops before rewind_session_context clears/rebuilds context. No rollback restores those effects when the second step returns false or raises.
- Suggested action: Add per-session transaction/rollback or explicit partial-state recovery.

## ISSUE-002: Context durability failure can still return ok=true.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: The handler always builds ok=true after both calls and only exposes context_ok in payload, so context_ok=false is success. The helper separately logs save_contexts/post_run failure but unconditionally returns true after that persist block.
- Suggested action: Fail or return explicit partial status unless context durability is confirmed.

## ISSUE-003: session_id is an unchecked path component.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: target_sid is only str/strip-normalized from params or request and is passed to rewind_session; its history/session helpers compose the id below the sessions root without rejecting absolute or parent traversal components.
- Suggested action: Validate the ID and enforce containment under sessions root.

## ISSUE-004: No direct handler tests cover rewind_context.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Repository search finds rewind_session_context helper cases in test_compact_partial.py but no direct or routed invocation of AgentWebSocketServer._handle_session_rewind_context.
- Suggested action: Test success, invalid input, no-agent, wire shape, and partial failures.

## ISSUE-005: Error codes are inconsistent.

- Dimension: `output_contract`
- Severity: `low`
- Status: `open`
- Evidence: Missing/invalid params and ValueError use BAD_REQUEST; the no-agent response calls _send_error_response without a code, and the generic exception payload contains only error text.
- Suggested action: Add stable codes such as AGENT_UNAVAILABLE and INTERNAL_ERROR.
