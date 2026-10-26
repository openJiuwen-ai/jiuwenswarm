---
id: ADR-0001
title: AgentServer Owns Prewarmed Session Identity
status: accepted
date: 2026-07-31
confidence: confirmed
modules:
  - agentserver-runtime
  - gateway-and-channels
  - agent-harness
flows:
  - session-prewarm-allocation
---

# ADR-0001: AgentServer Owns Prewarmed Session Identity

## Context

DeepAgent construction and interaction startup are expensive on the first message. Channel-generated IDs also create two owners for session identity and make a preinitialized, session-bound runtime impossible to claim safely.

## Decision

AgentServer is the sole allocator for new and fork target IDs. It maintains at most one speculative READY or warming single-Agent slot globally across prewarm-eligible enabled channel/project/work-mode keys; ACP and A2A are explicitly not eligible. Slots already carry their final session ID because OpenJiuwen runtime state depends on that identity. Channels obtain the ID through `session.create`; external protocol IDs are aliases.

Configuration validity uses a full SHA-256 fingerprint and a per-process boot ID. Warm misses return immediately and initialize in the background; the first real message awaits the same task. Team/Swarm modes remain outside this optimization.

The initial speculative candidate prefers Web/work/default-project, and a claimed key is prioritized for replenishment. A matching warming task is promoted to the foreground with its existing Session ID. Foreground entry cancels other speculative work and pauses dispatch of new slots. Foreground/background initialization shares a lock around process-global OpenJiuwen registries, preventing unsafe concurrent mutation while retaining foreground priority.

For eligible single-Agent sessions, final `work_mode` also determines runtime cache identity: `work` maps to `(agent, None)` and `code` maps to `(code, normal)`. AgentServer canonicalizes contradictory legacy Channel requests before metadata persistence and repeats the normalization on chat selection using locked Session state.

## Consequences

- Warm hits remove DeepAgent construction/startup from the first-message critical path.
- All creation callers require an AgentServer connection; Web/TUI local creation fallback is intentionally unavailable.
- Idempotency is process-local through `create_token`.
- Target metadata scales with eligible enabled channels times visible/default projects, but speculative runtime resources are globally capped at one READY/warming slot.
- Diagnostic Git and runtime-state probes run in a bounded worker lane and are not awaited by chat dispatch.
- Gateway prewarm sync is debounced/coalesced, and MemoryRail full reindex is skipped on first registration and singleflight on real embedding changes.
- A claimed READY Session cannot switch to a different root cache merely because a Channel sent stale `mode=agent` for a code project.
- Channel trajectory prediction and pool-size reduction can be layered later without changing identity ownership.

## Alternatives Rejected

- Rebinding a prewarmed runtime to a channel-generated ID: runtime state already depends on the original ID.
- Prewarming without `start_interaction`: leaves the expensive readiness boundary on `chat.send`.
- Sharing a generic runtime across projects: project workspace, rails, tools, and prompt state are part of initialization.
