---
id: agentserver-history-stream
name: AgentServer History Stream And Frontend Reconstruction
status: partial
confidence: confirmed
last_updated: 2026-07-14
user_visible_surface: "Web/TUI conversation restore and older-page loading."
source_of_truth:
  - "agent sessions directory/<session_id>/history.jsonl"
  - "agent sessions directory/<session_id>/history.json (legacy fallback)"
modules:
  - gateway-and-channels
  - agentserver-runtime
directories:
  - jiuwenswarm/channels/web/frontend/src
  - jiuwenswarm/channels/tui/frontend/src
  - jiuwenswarm/gateway
  - jiuwenswarm/server
code_symbols:
  - AgentWebSocketServer._handle_history_get_stream
  - AgentWebSocketServer.get_conversation_history
entrypoints:
  - jiuwenswarm/gateway/app_gateway.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/runtime/session/session_history.py
---

# AgentServer History Stream And Frontend Reconstruction

## Outcome

Web and TUI restore durable history with newest-first server pagination and oldest-first rendering. Gateway acks immediately; records, errors, and completion arrive separately as events used to rebuild messages and replay state.

## Causal Path

1. Web subscribes via `beginHistoryRestore`/`fetchHistoryPage` before requesting `{session_id,page_idx}`. TUI installs a page-done resolver before its request.
2. Web/TUI routes both forward `history.get` and register a local handler. `_normalize_gateway_message` forces upstream streaming even if the client omits `is_stream`.
3. Gateway enqueues for `MessageHandler`, then locally returns `{accepted:true,session_id,page_idx}` under the client request id. This ack confirms acceptance, not history success.
4. `MessageHandler` starts an E2A stream. `WebSocketAgentServerClient` queues by `request_id`, rejects duplicate in-flight ids, serializes sends, and yields correlated chunks through completion.
5. AgentServer dispatches to `_handle_history_get_stream`. `get_conversation_history` validates a non-empty string id and positive integer page, selects the session history file, and loads all records.
6. It filters restorable records, computes 20-record pages, reverses them so page 1 is newest, slices the page, and sanitizes selected records.
7. Each record is an incomplete `history.message` chunk with session/page/total metadata and ascending sequence. A complete `history.message` with `status:done` follows at sequence `len(messages)`. Invalid/missing history yields one complete `chat.error`.
8. Gateway turns chunks into routed channel events. Web filters by active generation/session/page, reverses arrival with `unshift`, and builds presentation models. TUI accumulates pages, sorts by time, coalesces assistant/tool fragments, and merges restored entries with live-only entries.

## State Classification

- **Durable truth:** preferred per-session `history.jsonl`; `history.json` is legacy-mode truth or fallback when JSONL is absent.
- **Transport:** request maps, E2A queue/task, websocket indexes, ack promise, sequences, and done resolvers are transient.
- **Derived:** filtered/reversed pages, sanitized records, Web generations, TUI buffers, replay models, pagination, and merged transcripts.
- **Live-only:** frontend entries absent from history survive merge but are not storage truth.
- **Observability:** counts/errors in logs are not replay checkpoints.

## Replay, Restore, Or Reconstruction

- History writes are queued and persisted per session; restart reconstructs without an in-memory Agent.
- JSONL is preferred by default. Legacy mode reverses preference; either mode falls back when its preferred file is absent. If both exist, only the selected file is read.
- Web initially loads page 1 and fetches older pages on demand. TUI sequentially fetches all reported pages before final merge.
- A new Web generation disposes its predecessor. A TUI request token abandons superseded loops; an invalid later page stops pagination while preserving fetched data.
- Existing empty history is valid page 1 with `total_pages:1`, no data chunks, then `done`.

## Contract

- **Request:** unary client method `history.get` with non-empty `session_id` and positive integer `page_idx`; Gateway converts it to upstream streaming and also returns a local ack.
- **Ack:** `{accepted:true,session_id,page_idx}`. Consumers retain compatibility with older acks containing `messages` and pagination metadata.
- **Data/done:** `history.message` carries one sanitized message plus exact session/page/total; completion is the same event with `status:done` and complete chunk. Consumers accept legacy end markers.
- **Failure:** invalid page/session, missing file, and caught load failure collapse to complete `chat.error`: `invalid page_idx or session history not found`.
- **Filter:** user records require text/media; untyped records require text; typed assistant records must be in the final/tool/usage/file/team/context restore allowlist.
- **Sanitize:** strings cap at 16 KiB, lists/tuples at 100, depth at 8, record at 64 KiB. Oversized records collapse to identity metadata, abbreviated content/event fields, and `truncated:true`.

## Consumer State And Output

Web normalizes roles/content/media, attaches files, and replays tool, usage, team, and harness state. It filters by session and optional page; missing-session frames are accepted only as end markers. The request/ack promise uses the web client's default 15-second timeout, but the event subscription has no independent done timeout.

TUI records metadata even on `done` and waits at most 15 seconds per page. Timeout resolves rather than failing, so output may be partial. It finally sorts by timestamp, coalesces assistant/tool groups, and lets restored ids override matching live entries.

## Failure, Ordering, And Identity

- Forwarding starts before ack, but processing is asynchronous; ack and first event have no guaranteed order. Subscribe-before-request prevents early loss. An accepted ack can be followed by `chat.error`.
- E2A correlates by `request_id`; frontend filters mainly by generation plus session/page. Duplicate concurrent requests for the same session/page are indistinguishable at the payload boundary.
- Stream receive has no pre-completion timeout, so missing completion can retain a Gateway task. TUI has a partial-result fallback; Web needs lifecycle disposal or an end marker after ack.
- `_session_dir` directly joins caller `session_id` below the sessions root. This flow only trims/non-empty-checks it; no resolve/containment or strict `sess_` validation was found. `..`/absolute-path behavior needs hardening and tests.
- Each page reads/parses the whole file, filters all records, and creates a reversed copy. TUI repeats this for every page; JSONL itself uses `read_text().splitlines()`. Its comment says 50 records/page, but the server constant is 20.
- Reads do not take the write lock. A partial JSONL line is skipped; this handler bypasses the retrying session reader. A legacy truncate/write race can look like valid empty history. Pages have no shared snapshot/version while writes continue.

## Verification

Static inspection confirmed route/ack, forced streaming, E2A correlation, AgentServer filtering/pagination/sanitization/chunks, file selection, and both frontends. `test_history_payload_limits.py` verifies a large record stays within 64 KiB. Gateway tests cover TUI registration/timeout policy; E2A tests cover normalization and event wire round-trip. Tests were not executed.

## Known Gaps

- No located test drives Web/TUI -> ack -> Gateway stream -> real file -> ordered reconstruction or asserts record/done/error sequences.
- No frontend test covers stale generations, duplicate pages, missing done, partial timeout, all-page ordering, or legacy ack.
- No containment/traversal, append/read race, cross-page mutation, malformed JSONL tail, or legacy truncate-window test covers this handler.
- Whole-file work per page can make TUI all-page restore quadratic; no cursor/index/snapshot or history byte cap exists.
- The generic error hides permission/corruption/I/O causes, and Web lacks a post-ack completion deadline.
