---
id: runtime-session
name: Runtime Session
confidence: confirmed
last_updated: 2026-09-09
read_when: "Working on product Session execution registration, scheduling, control input, cancellation, generation isolation, user-channel runtime ownership, or the Session unification migration."
---

# Runtime Session

## Responsibility

Owns the process-local product Session execution lifecycle behind `AgentRuntime`: registration, per-Session chat scheduling, Goal and control execution registration, exact interaction delivery, execution lookup, bounded terminal retention, cancellation/wait, generation-safe close, stream producer ownership, and Runtime shutdown ordering. Durable metadata and history remain in the existing stores.

## Current Boundary

- Foreground `agent.work.normal` and `agent.code.normal` chat, Goal, Goal attach, follow-up, steer, and interaction-answer requests use this Runtime from Process CLI and AgentServer; Web, TUI, ACP, and IM transports share that AgentServer boundary without owning Session execution state.
- Plan, Team, and background requests retain their distinct executors until their modules adapt.
- Runtime schedules ordinary chat in the Session lane. Goal stream/control/attach and control input are registered executions but run outside that lane because Goal scheduling, output lease, and `control_lock` remain owned by the Goal SDK. The facade continues to own history, MCP, memory, A2UI, and adapter semantics.
- An interaction event records its concrete request/interaction ID and moves an ended output round to `WAITING_FOR_CONTROL`. Its answer claims only the execution carrying that exact ID, records the parent relationship, and enters the existing adapter without creating a second chat turn or persistence owner. Goal and chat interactions may therefore coexist safely in one Session.
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

Deterministic tests cover Work/Code unary and stream routing, Goal classification, Goal/chat concurrency, Goal close cancellation, exact interaction matching, same-Session chat ordering, cross-Session concurrency, repeated interaction suspension, stale-control rejection, external stream cancellation, stream early close, resistant cancellation, generation isolation, ContextVar propagation, and shutdown. Configured-model system gates complete real chat and Goal `ask_user` rounds whose first streams end before the answer, then verify the resumed model response and terminal execution state.
