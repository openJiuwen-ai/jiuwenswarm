---
symbol: AgentWebSocketServer._handle_session_rewind_full
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_rewind_full audit evidence

## ISSUE-001: Compact-from rebuild omits the new summary records.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, rewind_session_context runs before compact_boundary, rewind_summary, and compact_summary are queued. Existing helper tests prove those records are inputs to context reconstruction, so the rebuilt context omits the records created by this request.
- Suggested action: Append compact records before rebuilding context.

## ISSUE-002: Context convergence failure still returns success.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, an unresolved rewind agent leaves rewind_context false, and a rewind_session_context exception is only logged; both paths still build an ok: true response after the durable rewind mutation.
- Suggested action: Return failure or explicit partial status unless durability is confirmed.

## ISSUE-003: Cross-store rewind is non-transactional.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, optional file restore runs first, history/diff mutation runs next, context reconstruction is best-effort, and compact-from records are queued last. No rollback spans these stores when a later step fails.
- Suggested action: Add prevalidation and atomic commit/rollback.

## ISSUE-004: Invalid compact inputs bypass the intended BAD_REQUEST contract.

- Dimension: `input_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, any compact direction other than exactly up_to enters ordinary rewind, while compact records are appended only for exactly from. Also summarized_count is converted with int() before the validation and mutation try/except, so malformed input escapes the handler instead of producing BAD_REQUEST.
- Suggested action: Validate compact fields before mutation in the BAD_REQUEST path.

## ISSUE-005: session_id is an unchecked path component.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, target_sid is only stripped before being passed into session/file helpers. The session lifecycle flow confirms that strict session-id normalization and resolved-path containment are not enforced at this AgentWebSocketServer boundary.
- Suggested action: Validate the ID and enforce containment under sessions root.

## ISSUE-006: No direct tests cover the three rewind modes.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: tests/unit_tests/test_compact_partial.py exercises helper behavior, including reconstruction from pre-existing compact summary records. No direct _handle_session_rewind_full or routed rewind tests were found for its three modes, invalid inputs, partial failures, or response contract.
- Suggested action: Test all modes, invalid inputs, missing agents, and failures.
