---
id: CHG-20260908-001
title: Cut Process CLI over to managed-only Session execution
type: refactor
date: 2026-09-08
modules:
  - runtime-session
  - agent-harness
flows:
  - runtime-session-reference-chain
confidence: confirmed
---

# Cut Process CLI Over To Managed-Only Session Execution

## What Changed

The Process CLI no longer reads a legacy Session-runtime environment switch. It always creates a managed `AgentRuntime` and rejects modes outside the Work Normal and Code Normal reference scope instead of silently falling back.

The facade exposes one direct unary executor that bypasses its `SessionManager` queue. Runtime calls that method and the existing stream directly; empty forwarding ports, unused lifecycle-participant scaffolding, dead migration values, implicit Session registration, and duplicate stream task branches were removed. An interaction event moves its execution to `WAITING_FOR_CONTROL` after the output round ends; the answer claims and resumes that execution outside the per-Session work lane, while the facade injects it through the existing adapter without starting another chat turn. Cancellation preserves semantic interrupt ordering and now wakes a stream consumer when an external execution cancellation stops its producer.

## Impact

- Process CLI Work/Code Normal have one scheduling and cancellation owner: `RuntimeSessionCoordinator`.
- User work remains serialized per Session, while answers required by that work cannot deadlock behind it.
- History, MCP, memory, A2UI, and stream post-processing remain facade-owned and are not duplicated.
- Managed Runtime Plan, Team, background, unregistered, and other unsupported execution is rejected until those paths exist.
- AgentServer and other unadapted Runtime callers retain their existing legacy behavior; this change does not cut them over.

## Verification

The focused managed/cancellation/stream suite covers control delivery while a stream is blocked, control cancellation isolation, stale-control rejection, and the original scheduling, stream, and lifecycle cases. The expanded Runtime, Process CLI, Adapter, and legacy SessionManager selection passed 273 of 274 tests; the only failure is the pre-existing Windows Chinese argparse encoding assertion. The configured-model system test passed both Work and Code two-turn Session resume scenarios.
