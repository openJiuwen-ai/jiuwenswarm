---
symbol: AgentWebSocketServer._handle_session_list
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_list`

## Actual Role

Scans the AgentServer sessions root, sorts all entries by directory mtime descending, skips non-directories, and cache-bust-reads each directory's metadata. Missing/corrupt metadata becomes a minimal directory-stat fallback. It softens any scan exception to a warning and returns the accumulated list as a successful, metadata-preserving E2A response under the shared send lock. Request filters, offsets, and limits are ignored.

## Key Signals

- Input: WebSocket, `AgentRequest`, and shared `send_lock` after `ReqMethod.SESSION_LIST`; only request id, channel, and metadata affect output.
- Output: `AgentResponse(ok=True, payload={"sessions": [...]})`, E2A-encoded and size-bounded under `send_lock`.
- Main side effects: Filesystem directory/stat reads, cache-busted metadata reads, warning logs, and WebSocket send.
- Main risks: duplicated semantics include internal heartbeat directories, ordering/fallback differs from the shared helper, one bad entry truncates the scan silently, and full scan/transfer is unbounded.
- Related tests: `test_session_metadata.py` covers the shared helper, not this handler; Gateway tests cover registration and timeout only. The session-lifecycle flow identifies directories and metadata as distinct durable sources. Tests were not run in this review.

## Detail Index

- Detail docs pending.
