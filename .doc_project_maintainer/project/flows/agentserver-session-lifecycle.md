---
id: agentserver-session-lifecycle
name: AgentServer Session Lifecycle
status: partial
confidence: confirmed
last_updated: 2026-08-03
user_visible_surface: "Session create, switch, list, fork, rewind, delete, history, and team session operations."
source_of_truth:
  - "agent session directories"
  - "session metadata"
  - "history records"
  - "OpenJiuwen checkpointer"
modules:
  - agentserver-runtime
  - agent-harness
directories:
  - jiuwenswarm/server
code_symbols:
  - AgentWebSocketServer._handle_session_create
  - AgentWebSocketServer._handle_session_fork
  - AgentWebSocketServer._handle_history_get_stream
entrypoints:
  - jiuwenswarm/server/agent_ws_server.py
---

# AgentServer Session Lifecycle

## Outcome

User and team session operations are exposed for create, register, switch, list, rename, delete, fork, rewind, compact, and history. New IDs and fork targets are allocated by AgentServer. Explicit IDs are rejected by normal creation; TUI startup may register a caller-supplied compatible ID through AgentServer and bypass prewarming.

## Causal Path

`_handle_message` routes session and history `ReqMethod` values to local handlers before generic chat handling. Session create validates project binding, claims or initializes a server-owned ID, and writes metadata; single-Agent claims can consume a prepared DeepAgent. TUI normal startup calls this method without `session_id` and waits for the returned ID before releasing queued RPCs. Its TUI-only explicit-ID compatibility branch validates the external ID, serializes creation per ID, treats existing metadata as authoritative, and bypasses prewarming. Fork requests omit the target ID and AgentServer allocates it before copying filesystem and runtime state. History, rewind, delete, and team operations retain their existing stores and behavior.

## State Classification

- Source of truth: session directories, metadata files, history records, checkpointer state.
- Runtime state: active agent/session instances, team managers, stream tasks.
- Derived output: paged and sanitized history payloads.

## Replay, Restore, Or Reconstruction

History paging rereads the full persisted history, filters restorable records, reverses them so latest records appear first, and slices a page. Fork and rewind reconstruct several stores independently; no transaction or recovery journal spans filesystem copies, history, checkpointer state, and active runtime state.

## Contract

`session.create` normally takes project/work/mode identity plus `create_token`; it returns `session_id`, normalized project binding, `prewarm_hit`, and `prewarm_status`. The TUI compatibility form instead accepts a `session_id` of at most 128 characters in the sanitized portable character set, returns `created`, resolved `mode`, and `prewarm_status="bypassed"`, and is idempotent. Other channels still reject explicit IDs, and other handlers that accept existing IDs retain their path-boundary review requirements.

## Verification

Focused warm-pool, AgentServer session, TUI Gateway/frontend, rewind, and fork tests cover allocation and registration mechanics. On 2026-08-03 the selected Python suite passed 114 tests in each prewarm state, and the TUI typecheck/build/full frontend test suite passed. The external `clear.spec.ts` reproduction has not yet been replayed after the startup-ordering fix.

## Known Gaps

Existing-session operations can still receive hostile IDs and require containment review. `create_token` idempotency is process-local, and fork can still leave partial state after later copy failure. Detailed downstream audits for metadata, history, checkpointer state, warm-resource limits, and team teardown remain pending.
