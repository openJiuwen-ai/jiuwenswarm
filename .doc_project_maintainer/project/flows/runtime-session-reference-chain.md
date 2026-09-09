---
id: runtime-session-reference-chain
name: Runtime Session Reference Chain
status: current
confidence: confirmed
last_updated: 2026-09-09
user_visible_surface: "Work Normal and Code Normal chat, Goal, and interaction control across Process CLI, Web, TUI, ACP, and IM channels."
source_of_truth:
  - "RuntimeSessionCoordinator process-local Session records"
  - "SessionExecutionRegistry execution records"
  - "existing Session metadata and history stores"
modules:
  - runtime-session
  - agent-harness
directories:
  - jiuwenswarm/runtime/session
  - jiuwenswarm/channels/process_cli
  - jiuwenswarm/server
entrypoints:
  - jiuwenswarm/channels/process_cli/client.py
  - jiuwenswarm/runtime/service.py
---

# Runtime Session Reference Chain

## Outcome

Work Normal and Code Normal share one transport-neutral Session runtime from the in-process Process CLI and AgentServer. Ordinary chat and Goal work therefore use the same execution registry, cancellation, control-input, stream-ownership, cleanup, and shutdown path across Web, TUI, ACP, and IM while preserving existing Session IDs, events, metadata, history, and facade preprocessing.

## Causal Path

```text
InProcessRuntimeClient
or AgentServer (Web / TUI / ACP / IM)
  -> AgentRuntime
  -> RuntimeSessionCoordinator
  -> SessionExecutionRegistry
     + SessionWorkScheduler (ordinary chat only)
  -> JiuWenSwarm facade
  -> Deep/Code Adapter
  -> RuntimeEvent
```

`create_or_resume_session` first delegates durable creation/resume to `AgentManager` and then registers an in-memory generation. Product `session.create` and switch flow through `AgentRuntime` provisioning; their successful commit registers eligible single-Agent Sessions, while direct callers are adopted at the Runtime execution boundary. Every owned call registers an execution. Ordinary chat enters the per-Session work lane; Goal set/resume, Goal get/pause/clear, Goal attach, follow-up, steer, and matched interaction input execute directly because their concurrency contract belongs to the Goal SDK and active adapter. Early consumer close and external execution cancellation both wake and await the producer. Cancellation invokes existing semantic interruption, then cancels the matching Runtime execution and waits within the bound. Session cleanup closes the active generation before existing adapter-resource cleanup; Runtime close stops the Coordinator before shared Agent resources.

## Interaction Control

An `ask_user` answer belongs to the interrupted execution and is not new Session work. The Coordinator stores the concrete `request_id` or `interaction_id` emitted by the event. `AgentRuntime` routes the later interrupt-resume payload to `RuntimeSessionCoordinator.deliver_control`, which claims only the matching execution, records the parent relationship, and executes outside the chat lane. The facade's `deliver_control_input` sends the answer through the existing adapter resume path without repeating normal-turn history, memory, or A2UI preprocessing; the answer stream owns the resumed output and final persistence. This rule is identical for chat and Goal and never falls back to the latest Session execution.

## Goal Ownership

Session Runtime owns Goal execution identity, state, cancellation, generation, and Session close. The Goal SDK keeps `control_lock`, Goal scheduling, and output-lease semantics. Moving that lock into Session Runtime would duplicate Goal state and serialize unrelated chat; keeping the boundary explicit allows a Goal execution to remain active while ordinary chat uses its Session lane.

## State And Replay

Coordinator records are deliberately process-local. Existing metadata/history remain durable truth, so a later Process CLI invocation can resume the same Session ID and reconstruct agent context without restoring the earlier execution registry. A new Runtime registration creates a new generation; late cleanup from an older detached lane cannot remove the new generation.

## Migration Boundary

There is no single-Agent legacy fallback. Foreground Work/Code Normal uses the Runtime from Process CLI and AgentServer, while Plan, Team, and background work retain distinct owners until their later migrations. The in-process reference chain remains permanent.

## Verification

- Deterministic: Runtime Session and reference tests pass for Goal classification, direct Goal execution, Goal/chat concurrency, exact interaction matching, Goal close cancellation, chat scheduling, repeated interaction suspension, stale-control rejection, cancellation, ContextVar, generation, stream early-close, shutdown, Work/Code unary/stream, and semantic-interrupt ordering.
- Compatibility: affected Runtime, AgentServer, Gateway, Web transport, IM transport, ACP, and TUI boundaries retain their protocols and pass their focused suites.
- Real model: `tests/system_tests/test_process_cli_session_runtime_live.py` passes two-turn resume for Work and Code, plus chat and Goal `ask_user` rounds where the first output stream ends, the answer resumes the exact DeepAgent interaction, and the model returns the expected terminal response.
