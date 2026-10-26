---
symbol: AgentWebSocketServer._handle_history_get
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_history_get`

## Actual Role

Handles the unary `history.get` path. It normalizes non-dict params to empty, passes `session_id` and `page_idx` to the synchronous history helper, maps either a page dict or `None` to one `AgentResponse`, encodes it, and sends one locked wire frame. Lookup, format preference, filtering, newest-first pagination, and record sanitization live in `get_conversation_history`; the underlying read helpers also create the derived session directory.

## Key Signals

- Input: WebSocket, `AgentRequest.params.session_id`, `page_idx`, and send lock.
- Output: `ok=true` payload `{messages, total_pages, page_idx}`, or one uncoded `ok=false` error for every invalid/missing/load-failure case; request metadata is not copied to the response.
- Main side effects: synchronously reads history, may create a session directory during lookup, and sends a WebSocket frame.
- Main risk: Caller-controlled session paths are unchecked, while every page reload scans the whole history.
- Test evidence: helper sanitization plus Gateway/E2A routing tests exist; direct unary-handler tests were not found and no tests were run for this documentation-only re-audit.
- Related flows: `agentserver-history-stream` confirms the same storage, whole-file pagination, path, race, and generic-error risks; `agentserver-session-lifecycle` classifies history as durable session state.

## Detail Index

- Detail docs pending.
