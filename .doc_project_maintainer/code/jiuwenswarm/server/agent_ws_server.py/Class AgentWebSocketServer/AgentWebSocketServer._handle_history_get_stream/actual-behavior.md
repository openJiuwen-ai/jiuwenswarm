---
symbol: AgentWebSocketServer._handle_history_get_stream
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_history_get_stream`

## Actual Role

Handles streamed `history.get` by synchronously loading, sanitizing, and reverse-paging persisted history through `get_conversation_history`, then sending one nonterminal `history.message` chunk per record and a terminal `history.message status=done`. Invalid input/missing history emits a terminal `chat.error`; an oversized record is replaced by the bounded-send helper and causes an early return without the normal done chunk.

## Key Signals

- Input: `params.session_id` and positive integer `params.page_idx`; validation is delegated.
- Output: Sequenced E2A record chunks plus a done chunk; alternatively one terminal `chat.error`, or an oversized fallback without history done.
- Main side effects: may create a session directory during a read and sends websocket frames under a per-send lock.
- Main risk: unsafe creating path reads, blocking full-history work, and inconsistent terminal frames can break page finalization.
- Related tests: Helper payload-limit, Gateway routing, and wire-codec tests are indirect. No direct valid/error/oversized handler sequence test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
