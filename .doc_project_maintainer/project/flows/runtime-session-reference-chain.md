---
id: runtime-session-reference-chain
name: Runtime Session Reference Chain
status: current
confidence: confirmed
last_updated: 2026-09-09
user_visible_surface: "Work Normal and Code Normal execution and interaction control across Process CLI, Web, TUI, ACP, and IM channels."
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

Work Normal and Code Normal share one transport-neutral Session runtime from the in-process Process CLI and AgentServer. Web, TUI, ACP, and IM transports therefore use the same scheduling, execution-registry, cancellation, control-input, stream-ownership, cleanup, and shutdown path while preserving existing Session IDs, events, metadata, history, and facade preprocessing.

## Causal Path

```text
InProcessRuntimeClient
or AgentServer (Web / TUI / ACP / IM)
  -> AgentRuntime
  -> RuntimeSessionCoordinator
  -> SessionWorkScheduler + SessionExecutionRegistry
  -> JiuWenSwarm facade
  -> Deep/Code Adapter
  -> RuntimeEvent
```

`create_or_resume_session` first delegates durable creation/resume to `AgentManager` and then registers an in-memory generation; AgentServer can explicitly register a product-owned Session after its own create, switch, or adoption flow. Unary and streaming calls register an execution before entering the per-Session work lane. Runtime invokes the facade's direct unary executor, so the old facade queue is not entered; streaming uses the existing bounded producer directly. Early consumer close and external execution cancellation both wake and await the producer. Cancellation invokes existing semantic interruption, then cancels the matching Runtime execution and waits within the bound. Session cleanup closes the active generation before existing adapter-resource cleanup; Runtime close stops the Coordinator before shared Agent resources.

## Interaction Control

An `ask_user` answer belongs to the interrupted execution and is not new Session work. The actual DeepAgent output round ends after emitting `chat.ask_user_question`, so the Coordinator records `WAITING_FOR_CONTROL` while releasing the serialized work lane. `AgentRuntime` routes the later interrupt-resume payload to `RuntimeSessionCoordinator.deliver_control`, which atomically claims the waiting execution, records the parent relationship, and executes outside the work lane. The facade's `deliver_control_input` sends the answer through the existing adapter resume path without repeating normal-turn history, memory, or A2UI preprocessing; the answer stream owns the resumed output and final persistence. A new ordinary work request supersedes a stale waiting interaction.

## State And Replay

Coordinator records are deliberately process-local. Existing metadata/history remain durable truth, so a later Process CLI invocation can resume the same Session ID and reconstruct agent context without restoring the earlier execution registry. A new Runtime registration creates a new generation; late cleanup from an older detached lane cannot remove the new generation.

## Migration Boundary

There is no single-Agent legacy fallback. Foreground Work/Code Normal uses the Runtime from Process CLI and AgentServer, while Plan, Team, and background work retain distinct owners until their later migrations. The in-process reference chain remains permanent.

## Verification

- Deterministic: Runtime Session and reference tests pass for scheduling, running-stream and ended-output-round control delivery, repeated interaction suspension, waiting-control supersession, control cancellation isolation, stale-control rejection, cancellation, ContextVar, generation, stream early-close, shutdown, Work/Code unary/stream, direct executor routing, explicit unsupported-mode rejection, and semantic-interrupt ordering.
- Compatibility: affected Runtime, AgentServer, Gateway, Web transport, IM transport, ACP, and TUI boundaries retain their protocols and pass their focused suites.
- Real model: `tests/system_tests/test_process_cli_session_runtime_live.py` passes two-turn resume for Work and Code, plus a Web-channel `ask_user` round where the first output stream ends, the answer resumes the same DeepAgent interaction, and the model returns the expected terminal response.
