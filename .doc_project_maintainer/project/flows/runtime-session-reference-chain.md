---
id: runtime-session-reference-chain
name: Runtime Session Reference Chain
status: current
confidence: confirmed
last_updated: 2026-09-08
user_visible_surface: "Process CLI Work Normal and Code Normal execution with persistent Session resume."
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
entrypoints:
  - jiuwenswarm/channels/process_cli/client.py
  - jiuwenswarm/runtime/service.py
---

# Runtime Session Reference Chain

## Outcome

The in-process Process CLI uses only the product-level Session runtime without changing AgentServer production routing. Work Normal and Code Normal share one transport-neutral scheduling, execution-registry, cancellation, stream-ownership, cleanup, and shutdown path while preserving existing Session IDs, events, metadata, history, and facade preprocessing. Unsupported Process CLI modes fail explicitly instead of entering a legacy execution path.

## Causal Path

```text
InProcessRuntimeClient
  -> AgentRuntime
  -> RuntimeSessionCoordinator
  -> SessionWorkScheduler + SessionExecutionRegistry
  -> JiuWenSwarm facade
  -> Deep/Code Adapter
  -> RuntimeEvent
```

`create_or_resume_session` first delegates durable creation/resume to `AgentManager` and then registers an in-memory generation; execution never registers one implicitly. Unary and streaming calls register an execution before entering the per-Session lane. Runtime invokes the facade's direct unary executor, so the old facade queue is not entered; streaming uses the existing bounded producer directly. Early consumer close and external execution cancellation both wake and await the producer. Cancellation invokes existing semantic interruption, then cancels the matching Runtime execution and waits within the bound. Session cleanup closes the active generation before existing adapter-resource cleanup; Runtime close stops the Coordinator before shared Agent resources.

## State And Replay

Coordinator records are deliberately process-local. Existing metadata/history remain durable truth, so a later Process CLI invocation can resume the same Session ID and reconstruct agent context without restoring the earlier execution registry. A new Runtime registration creates a new generation; late cleanup from an older detached lane cannot remove the new generation.

## Migration Boundary

`AgentRuntime` still defaults to legacy for AgentServer and other unadapted owners. A managed Runtime has no per-request legacy fallback: it rejects Plan, Team, background, unregistered, and other unsupported execution. The Process CLI always creates a managed Runtime, and its reference chain remains permanent.

## Verification

- Deterministic: Runtime Session and managed reference tests pass for scheduling, cancellation, ContextVar, generation, stream early-close, shutdown, Work/Code unary/stream, direct executor routing, explicit unsupported-mode rejection, and semantic-interrupt ordering.
- Compatibility: existing Runtime and facade SessionManager lifecycle tests pass; Process CLI timeout, interaction, and cleanup behavior remains green.
- Real model: `tests/system_tests/test_process_cli_session_runtime_live.py` passes two turns with the same Session ID for both `agent.work.normal + work` and `agent.code.normal + code`, with non-empty terminal output, increased history, and no active execution after cleanup.
