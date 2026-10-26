---
symbol: AgentWebSocketServer._handle_session_fork
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_fork audit evidence

## ISSUE-001: Source and target IDs can escape the sessions directory.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Lines 6327-6336 only stringify, trim, and require non-empty IDs. fork_session joins both values directly under get_agent_sessions_dir(); absolute and parent-traversing values can escape that root, and target_dir.mkdir(parents=True) plus history/metadata helpers then create or write the escaped target.
- Suggested action: Enforce the normalized session-ID grammar and verify resolved source/target containment beneath the canonical sessions root before any filesystem or checkpointer operation.

## ISSUE-002: Fork is neither atomic nor necessarily complete on success.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: fork_session creates the target before copying, swallows history-copy failure, and only enqueues metadata to a background writer whose later failure is logged. Lines 6351 and 6362-6367 ignore the Boolean results from context/state copy helpers, which return False on missing or failed copy, yet lines 6369-6374 report ok=true; a later raised exception leaves a target directory that blocks retry.
- Suggested action: Stage and verify history, metadata, context, state, and plan copies before a single commit; await durable metadata, roll back on failure, or return explicit per-store partial status that prevents automatic switching.

## ISSUE-003: State may be copied through the wrong agent variant.

- Dimension: `dependency_coupling`
- Severity: `high`
- Status: `open`
- Evidence: Line 6347 calls get_agent_nowait with channel_id only. AgentManager then returns the first matching cached variant (with a fallback preference for mode 'agent'), even though fork_session reads source mode and channel_metadata/project identity. Context can therefore be read from the wrong DeepAgent, and its card is used to address checkpointer state; when no agent exists, a generic fallback card is used and a missing-state False result is ignored.
- Suggested action: Resolve the exact source mode/sub-mode/project/card identity from authoritative session metadata, or copy durable state independently of an arbitrary live agent.

## ISSUE-004: Full-history filesystem work runs on the event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 6338-6344 call synchronous fork_session before the first await. It reads and rewrites the entire history, creates directories, scans up to 500 session metadata entries for title uniqueness, and can copy plan files synchronously inside the later async state helper.
- Suggested action: Run bounded filesystem work in a worker/job and avoid full-history materialization or broad metadata scans on the shared event loop.

## ISSUE-005: No handler-level fork contract test was found.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: Three fork_session tests cover channel_metadata copying, and separate state-helper tests cover selected transformation/plan cases. Static search found no _handle_session_fork test and no coverage for context copy, hostile IDs, ownership, active-source consistency, agent selection, async metadata failure, ignored False results, rollback, response codes, or Gateway/TUI switching after degradation.
- Suggested action: Add end-to-end handler/Gateway tests for success identity, active-source snapshotting, authorization/containment, every failed stage and rollback, response codes, and client switching behavior.

## ISSUE-006: A live source session is copied at inconsistent points in time.

- Dimension: `state_mutation`
- Severity: `high`
- Status: `open`
- Evidence: The handler does not lock, pause, or reject an in-flight source session. It copies persisted history synchronously, then awaits an in-memory context copy, then flushes/reads checkpointer state; source processing can advance between those stages. The Gateway /branch path cancels old-session work only after the fork response, while only the TUI client has a local busy check.
- Suggested action: Acquire a per-source session lifecycle lock, quiesce or reject active work server-side, and derive history/context/state from one named checkpoint or revision before publishing the target.

## ISSUE-007: The handler does not authorize access to the source session.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: The request supplies source_session_id directly, but lines 6326-6367 do not compare request identity/permission_context with source metadata user_id, channel ownership, project scope, or tenancy. Any caller able to reach session.fork and guess another session ID can copy its persisted history, metadata, in-memory context, and checkpointer state into a caller-chosen target.
- Suggested action: Resolve the authenticated principal and enforce source ownership plus channel/project tenancy before reading any source store; authorize the target namespace separately.
