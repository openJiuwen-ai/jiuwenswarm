---
symbol: AgentWebSocketServer._handle_command_btw
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_btw audit evidence

## ISSUE-001: Long-running /btw calls have no handler-level timeout or cancellation alignment.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, the handler awaits generate_btw_answer without a local timeout. Existing boundary configuration gives TUI requests a nominal 120 seconds while Gateway subtracts grace and caps TUI unary waits at 55 seconds, so the downstream caller can stop waiting before the AgentServer/model call has a matching deadline.
- Suggested action: Align client/Gateway/AgentServer deadlines and propagate cancellation to the BTW model call.

## ISSUE-002: Unknown adapter statuses are returned as successful transport payloads.

- Dimension: `output_contract`
- Severity: `low`
- Status: `open`
- Evidence: At HEAD 39feee89, any dict result is returned with ok=true without validating status or answer/error fields, while TUI recognizes only ok, no_context, and failed. A non-dict fails at result_data.get and becomes ok=false, so structurally different adapter mistakes produce different transport semantics.
- Suggested action: Validate the result schema and normalize unknown statuses or fields to a failed response.

## ISSUE-003: INFO logging includes the first 100 characters of the user question.

- Dimension: `boundary_safety`
- Severity: `low`
- Status: `open`
- Evidence: At HEAD 39feee89, the command.btw received INFO log records question[:100], exposing user-provided side-query content to normal operational logs.
- Suggested action: Log metadata or question length instead, or move redacted content to debug logging.

## ISSUE-004: Core paths are tested, but boundary handoffs are not fully covered.

- Dimension: `test_coverage`
- Severity: `low`
- Status: `open`
- Evidence: Direct tests cover empty input, success, missing agent, adapter error, auto-harness mapping, no_context, and default session; system tests cover forwarding/frame shape. Project routing, channel/mode fallback, malformed params/question types, unknown status, log redaction, and timeout/cancellation remain uncovered.
- Suggested action: Add boundary tests for the remaining routing, schema, privacy-log, and timeout/cancellation behavior.

## ISSUE-005: Missing identity is silently redirected to default runtime context.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, blank session and channel values become default and an omitted mode becomes agent.plan before get_agent and generate_btw_answer. params is assumed dict-like and question is assumed string-like; only an empty trimmed question has an explicit validation response.
- Suggested action: Require canonical session/channel identity for context-bearing queries, validate params/question types, and make mode fallback an explicit documented contract.
