---
id: agentserver-session-lifecycle
name: AgentServer Session Lifecycle
status: partial
confidence: confirmed
last_updated: 2026-07-31
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

User and team session operations are exposed for create, switch, list, rename, delete, fork, rewind, compact, and history. New IDs and fork targets are now allocated by AgentServer; explicit new IDs are rejected and restoration uses `session.switch`.

## Causal Path

`_handle_message` routes session and history `ReqMethod` values to local handlers before generic chat handling. Session create validates project binding, claims or initializes a server-owned ID, and writes metadata; single-Agent claims can consume a prepared DeepAgent. Fork requests omit the target ID and AgentServer allocates it before copying filesystem and runtime state. History, rewind, delete, and team operations retain their existing stores and behavior.

## State Classification

- Source of truth: session directories, metadata files, history records, checkpointer state.
- Runtime state: active agent/session instances, team managers, stream tasks.
- Derived output: paged and sanitized history payloads.

## Replay, Restore, Or Reconstruction

History paging rereads the full persisted history, filters restorable records, reverses them so latest records appear first, and slices a page. Fork and rewind reconstruct several stores independently; no transaction or recovery journal spans filesystem copies, history, checkpointer state, and active runtime state.

## Contract

`session.create` takes project/work/mode identity plus `create_token`; it returns `session_id`, normalized project binding, `prewarm_hit`, and `prewarm_status`. It rejects explicit `session_id`. Other handlers still accept existing IDs and therefore retain their path-boundary review requirements.

## Verification

Focused warm-pool and Web create-token tests cover allocation mechanics. Existing ACP/session, delete, history, and mode tests remain relevant, but the ACP suite was not runnable in this environment because an OpenJiuwen team-runtime dependency is missing.

## Known Gaps

Existing-session operations can still receive hostile IDs and require containment review. `create_token` idempotency is process-local, and fork can still leave partial state after later copy failure. Detailed downstream audits for metadata, history, checkpointer state, warm-resource limits, and team teardown remain pending.
