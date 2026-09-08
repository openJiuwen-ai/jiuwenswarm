---
id: agentserver-session-lifecycle
name: AgentServer Session Lifecycle
status: partial
confidence: confirmed
last_updated: 2026-09-08
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

`_handle_message` routes session and history `ReqMethod` values to local handlers before generic chat handling. Session create validates project binding, claims or initializes a server-owned ID, writes metadata, and registers Work/Code Normal sessions with the shared `AgentRuntime`; session switch performs the same registration after product-owner preparation. TUI normal startup calls this method without `session_id` and waits for the returned ID before releasing queued RPCs. Its TUI-only explicit-ID compatibility branch validates the external ID, serializes creation per ID, treats existing metadata as authoritative, and bypasses prewarming. Work/Code Normal unary and stream execution is serialized and indexed by `RuntimeSessionCoordinator`; AgentServer does not also register those streams in its host task map. Interaction answers are delivered as control input to the active execution outside that serialized work lane. Fork requests omit the target ID and AgentServer allocates it before copying filesystem and runtime state. History, rewind, delete, Plan, Team, and background operations retain their existing stores and owners.

## State Classification

- Source of truth: session directories, metadata files, history records, checkpointer state.
- Runtime state: Coordinator-owned Work/Code Normal executions, active agent/session instances, team managers, and host-owned Plan/Team/background stream tasks.
- Derived output: paged and sanitized history payloads.

## Replay, Restore, Or Reconstruction

History paging rereads the full persisted history, filters restorable records, reverses them so latest records appear first, and slices a page. Fork and rewind reconstruct several stores independently; no transaction or recovery journal spans filesystem copies, history, checkpointer state, and active runtime state.

## Contract

`session.create` is a transport adapter over `AgentRuntime.prepare_session_create` and the prepare/commit/abort transaction in `RuntimeSessionProvisioner`. It normally takes project/work/mode identity plus `create_token`; it returns `session_id`, normalized project binding, `prewarm_hit`, and `prewarm_status`. The TUI compatibility form instead accepts a `session_id` of at most 128 characters in the sanitized portable character set, returns `created`, resolved `mode`, and `prewarm_status="bypassed"`, and is idempotent. Other channels still reject explicit IDs, and other handlers that accept existing IDs retain their path-boundary review requirements.

## Verification

Focused warm-pool, AgentServer session, TUI Gateway/frontend, rewind, and fork tests cover allocation and registration mechanics. On 2026-09-08 the Runtime and affected AgentServer/Gateway suite passed 360 tests, the broad channel/Gateway/TUI suite passed 1277 tests with one skip, all 12 user channel identities passed explicit Work/Code Runtime ownership tests, and the configured-model Work/Code two-turn resume gate passed 2 tests. The external `clear.spec.ts` reproduction has not yet been replayed after the startup-ordering fix.

## Known Gaps

Existing-session operations can still receive hostile IDs and require containment review. `create_token` idempotency is process-local, and fork can still leave partial state after later copy failure. Detailed downstream audits for metadata, history, checkpointer state, warm-resource limits, and team teardown remain pending.

The Session Runtime now owns foreground Work/Code Normal execution and active-execution control input for AgentServer traffic from Web, TUI, ACP, and IM channels. AgentServer's host task map remains only for Plan, Team, and background execution until those owners are adapted; Gateway delivery and persistent mutation ownership are unchanged.
