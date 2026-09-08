---
id: runtime-session
name: Runtime Session
confidence: confirmed
last_updated: 2026-09-08
read_when: "Working on product Session execution registration, scheduling, cancellation, generation isolation, Process CLI runtime ownership, or the Session unification migration."
---

# Runtime Session

## Responsibility

Owns the process-local product Session execution lifecycle behind `AgentRuntime`: registration, per-Session `LATEST_FIRST` scheduling, execution lookup, bounded terminal retention, cancellation/wait, generation-safe close, stream producer ownership, and Runtime shutdown ordering. Durable metadata and history remain in the existing stores.

## Current Boundary

- `AgentRuntime` defaults to `SessionManagementMode.LEGACY`, so AgentServer and other unchanged callers retain existing behavior.
- `InProcessRuntimeClient` always creates a managed Runtime; the Process CLI rollback switch has been removed.
- The Process CLI admits only `agent.work.normal` and `agent.code.normal` foreground single-Agent chat turns and rejects unsupported modes instead of falling back. Other Runtime callers retain their migration boundary until their module adaptation.
- Runtime calls the facade's direct unary executor and existing stream without an empty forwarding layer. The facade continues to own history, MCP, memory, A2UI, interrupt, and adapter semantics.
- Managed callers must create or resume a Session before execution. Unsupported modes fail at the Runtime boundary instead of falling back.

## Entry Points And Symbols

- `jiuwenswarm/runtime/session/coordinator.py`: `RuntimeSessionCoordinator` and the public run/cancel/close/query surface.
- `jiuwenswarm/runtime/session/work_scheduler.py`: `SessionWorkScheduler` and generation-owned lanes.
- `jiuwenswarm/runtime/session/execution_registry.py`: `SessionExecutionRegistry` and bounded indexes.
- `jiuwenswarm/runtime/session/model.py`: Session and Execution models/results.
- `jiuwenswarm/runtime/service.py`: managed routing, executor selection, and lifecycle ordering.
- `jiuwenswarm/channels/process_cli/client.py`: managed-only reference entry.

## Follow-On Ownership

Adapter, provisioning, repository, delivery, lifecycle-resource, Team, and background modules adapt in that dependency order. Lifecycle contracts are introduced with their first real implementation rather than kept as unused scaffolding. Once all callers use the Runtime facts, the facade `SessionManager`, AgentServer `_session_stream_tasks`, broad AgentManager scans, global product delivery route, prefix-based persistence inference, and migration modes are removed. The `InProcessRuntimeClient` reference chain remains permanent.

## Verification

Deterministic tests cover Work/Code unary and stream routing, same-Session ordering, cross-Session concurrency, external stream cancellation, stream early close, resistant cancellation, generation isolation, ContextVar propagation, shutdown, and legacy compatibility. A configured-model system gate completes two resumed Process CLI turns for both Work and Code and verifies terminal output, durable history growth, stable Session ID, and terminal execution state after cleanup.
