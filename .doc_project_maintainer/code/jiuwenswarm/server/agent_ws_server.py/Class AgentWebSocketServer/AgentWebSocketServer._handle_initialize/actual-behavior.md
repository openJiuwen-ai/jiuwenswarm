---
symbol: AgentWebSocketServer._handle_initialize
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_initialize`

## Actual Role

Coordinates a partially duplicated ACP handshake. It normalizes non-dict params to empty, accepts protocol/client capabilities without negotiation, stores ACP capabilities on the specific WebSocket, then calls the process-wide AgentManager. For acp the manager caches channel capabilities, destroys all shared ACP agents, creates a replacement code agent, and returns defaults; for other channels it returns None, which this method also converts to ACP defaults. Capability encoding/send remains in the mutation try, so transport failure is classified as initialize failure after shared state may already be replaced.

## Key Signals

- Input: Optional `clientCapabilities` and `protocolVersion`; shape/size/version are not validated, and complete client capabilities are logged at INFO.
- Output: One capability response or raw error. None from non-ACP initialization is indistinguishable from a real ACP default capability result; request metadata is not copied.
- Main side effects: Mutates WebSocket-scoped capability state, process-wide channel capability caches, and the shared ACP agent registry; cleanup/creation and response delivery are one broad failure domain.
- Main risk: Concurrent or retried handshakes can replace active ACP runtime, while caches and runtime remain partially committed on creation or delivery failure.
- Related evidence: Fake-manager happy/fallback tests and sequential WebSocket capability isolation exist; real destructive lifecycle, concurrency, invalid negotiation, transport ambiguity, and end-to-end handshake ownership remain untested. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
