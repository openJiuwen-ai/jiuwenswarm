---
symbol: AgentWebSocketServer._handle_initialize
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_initialize audit evidence

## ISSUE-001: ACP initialize replaces shared channel runtime without serialization.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, AgentManager.initialize cleans every process-wide acp agent, deletes the shared channel registry, and creates a replacement without an initialize lock. The connection handler schedules incoming frames concurrently, so overlapping initialize requests can interleave destructive cleanup/create.
- Suggested action: Make initialization connection-scoped and idempotent, or serialize an atomic replace.

## ISSUE-002: The response does not represent client-visible ACP initialization reliably.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, ACP WebSocket and stdio transport layers also own handshake behavior rather than consistently awaiting this RPC. AgentManager.initialize returns None for every non-ACP channel, but this handler replaces None with ACP_DEFAULT_CAPABILITIES and reports success, making unsupported/non-forwarded paths look initialized.
- Suggested action: Use one handshake owner, await real initialization, propagate failure, and reject unsupported channels.

## ISSUE-003: Failure leaves capability state partially committed.

- Dimension: `error_handling`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, ACP client capabilities are stored on the WebSocket before AgentManager.initialize, and AgentManager stores the shared channel capability cache before cleanup/create. Neither cache is rolled back if cleanup, creation, encoding, or send later fails.
- Suggested action: Validate first; commit caches only after successful creation or roll them back.

## ISSUE-004: Handshake fields lack schema and version negotiation.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, clientCapabilities and protocolVersion are accepted without type, size, or supported-version validation and the complete capabilities object is logged at INFO. The request default is string 0.1.0 while advertised fallback capabilities use a separate numeric protocol version representation.
- Suggested action: Validate bounded capabilities and negotiate one canonical version representation.

## ISSUE-005: Tests omit real lifecycle safety.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, direct tests use FakeAgentManager for manager/fallback capabilities, and a sequential two-WebSocket test verifies WebSocket-scoped metadata. No real-manager test covers non-ACP/invalid calls, concurrent initialize, rollback, send-after-commit failure, or disruption of active ACP work.
- Suggested action: Add real-manager lifecycle, concurrency, rollback, invalid-input, and end-to-end tests.

## ISSUE-006: Response delivery failure is treated as initialization failure after shared state is committed.

- Dimension: `error_handling`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, capability encoding and the success send are inside the same broad try as destructive AgentManager.initialize. If encoding/send fails after replacement succeeds, the except reports initialize failed and attempts a second error send, but does not restore the old ACP agent/capability caches or provide an idempotency token for a client retry.
- Suggested action: Separate committed initialization from transport delivery, make initialize idempotent/connection-scoped, and retain a replayable result keyed by request or connection.
