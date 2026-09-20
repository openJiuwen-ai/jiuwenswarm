---
id: CHG-20260907-001
title: Add the managed Runtime Session foundation and Process CLI reference chain
type: refactor
date: 2026-09-07
modules:
  - runtime-session
  - agentserver-runtime
  - agent-harness
flows:
  - runtime-session-reference-chain
  - agentserver-session-lifecycle
confidence: confirmed
---

# Add the managed Runtime Session foundation and Process CLI reference chain

## What Changed

A new `jiuwenswarm.runtime.session` package owns in-process Session execution models, indexes, scheduling, generation-safe cancellation/close, lifecycle participation, and streaming producer ownership. `AgentRuntime` now supports explicit legacy or managed Session routing and owns one Coordinator. The Process CLI selects managed routing by default for Work Normal and Code Normal, with an environment rollback to legacy. AgentServer still receives the default legacy Runtime.

The migration executor delegates into the existing facade, so no history, memory, MCP, A2UI, or adapter preprocessing was duplicated. Plan, Team, persistent mutations, delivery routing, and background scheduling remain outside this first slice.

## Why

Session execution and cleanup were split across AgentServer stream tasks, facade queues, AgentManager scans, and transport connection state. Establishing one Runtime-owned execution fact and a proven in-process chain lets module owners adapt incrementally without combining the risky production cutover with the foundational concurrency refactor.

## Impact

- User-visible protocol and persistence formats are unchanged.
- Process CLI Work/Code Normal now exercise the managed reference chain.
- AgentServer, Gateway, Team, Plan, and background production paths remain legacy until their staged adaptations.
- The reference chain and its tests remain after migration; compatibility executor and migration flags are deleted only in final cleanup.
- Deterministic and configured-model verification passed; one unrelated existing Windows Chinese argparse rendering assertion remains outside this change.
