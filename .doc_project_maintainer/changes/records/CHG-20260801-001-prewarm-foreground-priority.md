---
id: CHG-20260801-001
title: Prioritize foreground chat over background prewarming
type: fix
date: 2026-08-01
modules:
  - agentserver-runtime
  - gateway-and-channels
  - agent-harness
directories:
  - jiuwenswarm/server
  - jiuwenswarm/gateway
flows:
  - session-prewarm-allocation
  - gateway-agentserver-e2a-chat
code_symbols:
  - AgentWarmPool
  - AgentWarmPool.sync
  - AgentWarmPool.claim
  - AgentWarmPool.begin_foreground
  - AgentWarmPool.end_foreground
  - AgentWebSocketServer._handle_stream
  - AgentWebSocketServer._handle_unary
  - AgentWebSocketServer._handle_session_create
  - AgentWebSocketServer._prepare_code_mode_chat_turn
  - resolve_agent_request_mode
  - JiuWenSwarmDeepAdapter._schedule_runtime_state_write
  - JiuWenSwarmDeepAdapter._schedule_memory_reindex
decisions:
  - ADR-0001
commits: []
confidence: confirmed
---

# Prioritize Foreground Chat Over Background Prewarming

## What Changed

Foreground claimed-session initialization now uses an independent concurrency lane and chat-like AgentServer requests pause dispatch of new background warm slots. Startup reconciliation stores the full target set as pending keys but starts one background initialization by default, separated by a cooldown. Gateway and AgentServer both exclude ACP/A2A from warm targets.

Project discovery, runtime-state Git probes, and Git-ignore probing no longer run synchronously on the AgentServer event loop. Runtime-state writes are coalesced per adapter and bounded across worker threads.

The contention follow-up globally caps speculative READY/warming work at one slot, promotes a matching warming task instead of duplicating it, cancels other speculative tasks on foreground entry, and serializes OpenJiuwen process-global registry initialization. Gateway sync triggers are debounced/coalesced. MemoryRail skips first-registration full reindex and singleflights real configuration-change reindex per workspace/fingerprint.

The cache-identity follow-up maps code prewarming to `mode=code, sub_mode=normal`, canonicalizes eligible Session creation from final `work_mode`, and repeats that normalization from locked Session metadata on chat selection. This removes the `code:<empty>`, `code:normal`, and `agent:<empty>` split that caused a READY Session to create a second child on first input.

The CodeCheck follow-ups replace complex channel, pending, and stale-slot comprehensions plus a four-part cancellation condition with explicit loops and named fingerprint checks. They preserve reconcile and requeue behavior.

## Why

Live logs showed background DeepAgent preparation taking tens of seconds and synchronous Git/runtime-state probes executing in the same loop as `chat.send`. That allowed diagnostic or speculative work to delay user-visible initialization even though the warm pool used an asyncio semaphore.

## Impact

- User-visible: a real restarted-stack ham-snake Web session completed in about 7.5 seconds from allocated ID to final history record; the reproduced 30–40 second outlier did not recur.
- Internal: foreground work bypasses the background semaphore; new background slots pause during active chats; startup creates a bounded number of tasks.
- Tests: 139 focused AgentServer/runtime tests passed across the final code slices, including foreground priority, channel filtering, lazy dispatch, out-of-order reconcile protection, and non-blocking runtime probes.
- Follow-up tests: 179 focused tests passed across WarmPool, mode identity, AgentServer send/reload/ACP/plan mode, Gateway ACP, and adapter history/memory behavior.
- Final verification: Ruff passed and 167 relevant tests passed. The pipeline-only lightweight Adapter regression was fixed by treating a missing diagnostic-task slot as idle; the stable upstream root-cleanup expectation remains outside this change.
