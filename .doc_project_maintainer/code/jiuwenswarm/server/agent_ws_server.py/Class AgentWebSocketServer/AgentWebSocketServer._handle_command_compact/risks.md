---
symbol: AgentWebSocketServer._handle_command_compact
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_compact audit evidence

## ISSUE-001: Emits an apparently unconsumed context.compressed push.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, every result == compressed with truthy stats sends context.compressed before any history persistence. Inspected Web/TUI consumers subscribe to context.compression_state rather than this aggregate event, and no direct test asserts a consumer for context.compressed.
- Suggested action: Align event names or remove the redundant push and rely on tested RPC stats/context.compression_state delivery.

## ISSUE-002: Summary display depends on best-effort push delivery.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, the RPC returns compact_summary, but TUI suppresses local compact output when it is present and expects context.compression_state. send_push catches missing-connection and delivery failures and returns no status, so the handler still reports success without knowing whether the state event was delivered.
- Suggested action: Render an RPC fallback or make push delivery observable so summary display does not depend on best-effort transport.

## ISSUE-003: Important side effects and failure branches are under-tested.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Two direct tests cover a compressed response and one compression-state push; busy/noop, missing agent, adapter error, invalid or missing session, routing, push failure, history persistence failure, and partial post-compression failure remain uncovered. The custom-instructions-named test supplies instructions but does not assert they are consumed.
- Suggested action: Add focused tests for those result, error, routing, delivery, and persistence branches.

## ISSUE-004: Post-compression side-effect failure can report failure after context was already mutated.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, agent.compress_context completes before append_compact_history_records and the second push. If history append or payload construction raises, the broad except returns ok=false even though context compression and possibly the first push already occurred; the two history records are themselves appended separately with no rollback.
- Suggested action: Separate the committed compression result from best-effort notification/history outcomes, make persistence atomic, and return explicit partial-success state instead of a retry-inducing generic failure.

## ISSUE-005: Mutation targeting is silently defaulted and advertised compact instructions are ignored.

- Dimension: `input_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, a missing/blank session_id is replaced with default and then used for context mutation and history persistence. params is not type-checked, and only params.mode is consumed; params.instructions, supplied by the existing custom-instructions test/request shape, is never passed to compress_context or rejected.
- Suggested action: Require a canonical explicit session ID, validate params as an object, and either implement instructions through the compression API or reject the unsupported field.
