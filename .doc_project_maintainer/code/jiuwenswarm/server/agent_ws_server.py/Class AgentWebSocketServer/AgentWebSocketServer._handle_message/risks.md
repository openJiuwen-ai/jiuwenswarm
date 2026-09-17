---
symbol: AgentWebSocketServer._handle_message
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_message audit evidence

## ISSUE-001: Large method is the central routing table for many runtime RPC families.

- Dimension: `complexity`
- Severity: `medium`
- Status: `open`
- Evidence: Current lines 1341-1717 span 377 lines with 76 if nodes, 75 awaits, 68 req_method predicates, and 70 delegated-handler awaits; they combine decoding, compatibility conversion, ACP enrichment, hooks, dispatch, cancel orchestration, and error-wire sending.
- Suggested action: Separate decode/normalize, metadata enrichment, dispatch-table routing, and cancel orchestration behind tested helpers.

## ISSUE-002: Malformed non-JSON-error payloads can escape before normalized error handling.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Lines 1365-1399 convert E2A/fallback payloads before the guarded dispatch block at 1401. JSON scalars/lists make _payload_to_request call .get on a non-dict; missing fallback legacy data and unknown E2A/legacy req_method values also raise there without a normalized response.
- Suggested action: Validate decoded JSON is a dict and wrap E2A/legacy conversion in the same error-normalization path.

## ISSUE-003: Direct router coverage misses malformed converted payloads and many dispatch branches.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Direct tests cover open/closed invalid-JSON sends, closed unary handling, WebSocket-scoped ACP metadata, and disconnect-cancel cleanup/failure paths. None cover non-object JSON, invalid fallback envelopes, unknown methods, request-supplied ACP capability override, a cancellation-resistant stream task, or representative routing across the dispatch table.
- Suggested action: Add direct router tests for malformed E2A/legacy inputs, capability precedence, cancellation timeout, and selected high-risk ReqMethod branches.

## ISSUE-004: An ACP request can override connection-scoped client capabilities.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 1402-1409 copy request metadata and use metadata.setdefault('acp_client_capabilities', ws_caps or manager_caps). E2A channel_context becomes request metadata, so a request-provided key wins over capabilities recorded from INITIALIZE; the downstream adapter uses this value to register ACP filesystem/terminal tools.
- Suggested action: Treat the WebSocket-scoped INITIALIZE record as authoritative and assign the capability field rather than preserving an inbound override.

## ISSUE-005: Cancel handling can wait indefinitely for a cancellation-resistant stream task.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 1625-1659 cancel the per-session stream task and then await it without a timeout before disconnect runtime cleanup. asyncio cancellation is cooperative, so a task that suppresses CancelledError can stall the cancel response path and defer cleanup indefinitely.
- Suggested action: Bound the cleanup wait, log timeout diagnostics, and continue disconnect-scoped runtime cleanup even when the producer does not terminate.
