---
id: runtime-session
name: Runtime Session
confidence: confirmed
last_updated: 2026-09-09
read_when: "Working on product Session execution registration, scheduling, control input, cancellation, generation isolation, user-channel runtime ownership, or the Session unification migration."
---

# Runtime Session

## Responsibility

Owns the process-local product Session execution lifecycle behind `AgentRuntime`: registration, per-Session `LATEST_FIRST` work scheduling, active-execution control delivery, execution lookup, bounded terminal retention, cancellation/wait, generation-safe close, stream producer ownership, and Runtime shutdown ordering. Durable metadata and history remain in the existing stores.

## Current Boundary

- Foreground `agent.work.normal` and `agent.code.normal` requests use this Runtime from Process CLI and AgentServer; Web, TUI, ACP, and IM transports share that AgentServer boundary without owning Session execution state.
- Plan, Team, and background requests retain their distinct executors until their modules adapt.
- Runtime calls the facade's direct unary executor and existing stream without another scheduling layer. The facade continues to own history, MCP, memory, A2UI, and adapter semantics.
- An interaction event ends its output round and moves the execution to `WAITING_FOR_CONTROL`, releasing the work lane without losing resume ownership. Its answer claims that waiting execution, records the parent relationship, and enters the existing adapter without creating a second chat turn or persistence owner.
- Callers must create, resume, or explicitly register a Session before managed execution. Unsupported modes fail at the Runtime boundary instead of falling back.

## Entry Points And Symbols

- `jiuwenswarm/runtime/session/coordinator.py`: `RuntimeSessionCoordinator` and the public run/cancel/close/query surface.
- `jiuwenswarm/runtime/session/work_scheduler.py`: `SessionWorkScheduler` and generation-owned lanes.
- `jiuwenswarm/runtime/session/execution_registry.py`: `SessionExecutionRegistry` and bounded indexes.
- `jiuwenswarm/runtime/session/model.py`: Session and Execution models/results.
- `jiuwenswarm/runtime/service.py`: managed routing, executor selection, and lifecycle ordering.
- `jiuwenswarm/channels/process_cli/client.py`: in-process reference entry.
- `jiuwenswarm/server/agent_ws_server.py`: AgentServer registration and transport boundary.

## Follow-On Ownership

Adapter, provisioning, repository, delivery, lifecycle-resource, Team, and background modules adapt in that dependency order. Lifecycle contracts are introduced with their first real implementation rather than kept as unused scaffolding. Once all callers use the Runtime facts, the facade `SessionManager`, AgentServer `_session_stream_tasks`, broad AgentManager scans, global product delivery route, prefix-based persistence inference, and migration modes are removed. The `InProcessRuntimeClient` reference chain remains permanent.

## Verification

Deterministic tests cover Work/Code unary and stream routing, same-Session ordering, cross-Session concurrency, running-stream and ended-output-round control delivery, repeated interaction suspension, waiting-control supersession, control cancellation isolation, stale-control rejection, external stream cancellation, stream early close, resistant cancellation, generation isolation, ContextVar propagation, and shutdown. A configured-model system gate completes a real `ask_user` round whose first stream ends before the answer, then verifies the resumed model response and terminal execution state.
