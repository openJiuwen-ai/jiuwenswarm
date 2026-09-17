---
symbol: AgentWebSocketServer._handle_acp_tool_response
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_acp_tool_response audit evidence

## ISSUE-001: A response can complete another session's pending ACP request.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, complete_jsonrpc_response is called on the process-global AcpOutputManager using only caller-supplied params.jsonrpc_id. The handler does not bind or compare WebSocket, channel, session, request identity, method, or response body id; generic forwarding also makes acp.tool_response reachable outside a connection-scoped ACP callback.
- Suggested action: Bind entries to authorized connection, channel, session, and body id; require all to match.

## ISSUE-002: Malformed JSON-RPC payloads become successful tool responses.

- Dimension: `input_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, every non-dict params.response is coerced to {} before the pending lookup. A matching jsonrpc_id therefore consumes/completes the future and returns accepted=true without requiring jsonrpc=2.0, matching body id, or exactly one valid result/error field; downstream wrappers can interpret the empty body as success.
- Suggested action: Validate id and result/error schema before consuming the pending entry.

## ISSUE-003: Correlation state is split across Gateway and AgentServer registries.

- Dimension: `dependency_coupling`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, Gateway removes its jsonrpc_id-to-session correlation before forwarding, while AgentServer independently removes/completes AcpOutputManager pending state. Dispatch loss or ownership mismatch can consume one registry while the other side times out or can no longer route a retry.
- Suggested action: Use one owner or retain correlation state until AgentServer acceptance.

## ISSUE-004: Tests omit trust-boundary and lifecycle cases.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, test_agentserver_acp.py covers one valid completion and one unknown-id soft ignore. It does not cover wrong connection/channel/session ownership, missing/mismatched body id, malformed result/error schema, duplicate/racing responses, dispatch/ack loss, or payload bounds.
- Suggested action: Add ownership, schema, race, transport-loss, and payload-boundary tests.

## ISSUE-005: Pending completion is irreversible before acknowledgment delivery.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, complete_jsonrpc_response removes/completes global pending state before response encoding and the locked acknowledgment send. Encoding or transport failure is not handled locally and cannot restore the pending entry; a retry is then reported as unknown_or_late_response even though the first acknowledgment was never delivered.
- Suggested action: Make acceptance idempotently replayable until acknowledged, or couple pending completion and Gateway correlation through a single delivery-aware owner.
