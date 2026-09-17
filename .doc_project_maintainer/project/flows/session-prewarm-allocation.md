---
id: session-prewarm-allocation
name: Session Prewarm And Allocation
status: partial
confidence: confirmed
last_updated: 2026-08-03
user_visible_surface: "Low-latency creation of single-Agent work/code sessions across enabled channels."
source_of_truth:
  - "AgentServer session metadata"
  - "AgentServer process-local warm pool"
modules:
  - agentserver-runtime
  - gateway-and-channels
  - agent-harness
directories:
  - jiuwenswarm/server
  - jiuwenswarm/gateway
  - jiuwenswarm/channels
code_symbols:
  - AgentWarmPool
  - AgentWarmPool.sync
  - AgentWarmPool.claim
  - AgentWarmPool.begin_foreground
  - AgentWarmPool.end_foreground
  - AgentWebSocketServer._handle_agent_prewarm_sync
  - AgentWebSocketServer._handle_session_create
  - AgentWebSocketServer._handle_stream
  - AgentWebSocketServer._handle_unary
  - resolve_agent_request_mode
  - JiuWenSwarmDeepAdapter.prepare_session
  - JiuWenSwarmDeepAdapter.configure_session_runtime
  - JiuWenSwarmDeepAdapter._schedule_runtime_state_write
  - JiuWenSwarmDeepAdapter._schedule_memory_reindex
entrypoints:
  - jiuwenswarm/gateway/app_gateway.py
  - jiuwenswarm/server/agent_ws_server.py
---

# Session Prewarm And Allocation

## Outcome

Prewarming is on unless `JIUWENSWARM_AGENT_PREWARM` is set to `0`/`false`/`no`/`off`. While off, `sync` reports zero statistics and never schedules preparation, and every claim returns a freshly allocated ID with `prewarm_status="bypassed"`; the session initializes lazily on its first request. Allocation, `create_token` idempotency, and `session.create` validation stay in force, so the rest of this flow is unchanged and only its warm path is dormant.

While on, AgentServer keeps at most one speculative, unclaimed READY or warming DeepAgent across all eligible single-Agent work/code keys. Single-Agent identity is canonical: work uses `agent:<empty>:<project>` and code uses `code:normal:<project>` in prewarm, metadata, and chat selection. ACP and A2A never enter the target set. A warm hit avoids `create_instance` and `start_interaction` on the first `chat.send`.

Team, `code.team`, and Swarm creation bypass the warm pool.

## Causal Path

1. Gateway finishes channel registration and sends `agent.prewarm.sync` with deduplicated eligible channel IDs after filtering ACP/A2A. AgentServer repeats the exclusion defensively. Startup, configuration, and channel triggers are debounced and coalesced so reload completion is not followed by duplicate reconcile bursts; a periodic scan is the fallback.
2. AgentServer scans visible/default projects in a worker thread and stores one immutable `WarmKey` target snapshot. Reconciliation retains missing targets as pending keys, but globally permits only one speculative READY or warming slot; it does not create one asyncio task per project/channel at startup.
3. Background preparation gets the root adapter with `(code, normal)` for code or `(agent, None)` for work, creates the session child, configures stable runtime state, and starts the interaction without sending input.
4. `session.create` validates project binding, derives the single-Agent canonical mode from final `work_mode`, and persists that mode before allocation. A READY slot is atomically claimed; matching warming work is promoted with its existing ID.
5. AgentServer writes normal metadata only after claim. The prewarm marker is retained through the claim and removed only after metadata commits, closing the crash gap without exposing blank slots in normal session listings.
6. Chat selection reads the locked Session `work_mode` (falling back to the request), canonicalizes stale `mode=agent` code requests to `code.normal`, awaits the claimed task, and selects the same AgentManager cache key. Foreground cancellation and the shared registry lock prevent competing initialization.
7. MemoryRail registration does not schedule a full reindex on first registration. A real embedding-configuration change is singleflight per normalized workspace and fingerprint, preventing parallel new sessions from repeating the same repository-wide indexing work.
8. Web, TUI, IM, ACP, A2A, SSH, and single-Agent Cron use the returned ID. TUI constructs its boot-creation Promise before WebSocket callbacks can issue startup RPCs, releases it on `connection.ack`, and constructs queued frames only after the returned ID is installed; normal startup omits `session_id` and uses a stable `create_token`. ACP/A2A/SSH retain protocol IDs as Gateway aliases. Fork IDs are also AgentServer allocated but do not consume blank warm slots.

## State And Identity

- Source of truth: claimed session metadata and history under AgentServer ownership.
- Cache: READY slots, preparation tasks, `create_token` results, and external-channel aliases are process-local.
- Scheduling state: one target snapshot, at most one speculative READY/warming slot globally, pending/prioritized keys, promoted tasks, foreground count, and a shared initialization lock are process-local.
- Disposable state: `.prewarm/<session_id>.json` markers identify unclaimed slots and include `boot_id`.
- Revision: `boot_id + SHA-256(config/env) + sequence`; only matching current fingerprints may publish READY.
- ID format: `<channel>_<timestamp/random>`.

On startup, old-boot markers and unclaimed metadata-less directories are removed. Claimed/history sessions are not removed. Configuration changes stale only unclaimed slots; active sessions continue through existing reload behavior.

## Failure, Ordering, And Idempotency

- Project validation precedes allocation.
- Normal create rejects explicit IDs from other channels. TUI startup alone may pass an explicit ID to `session.create`; AgentServer logs and validates it under a per-ID lock, preserves existing binding, and returns `prewarm_status="bypassed"` without a warm claim.
- `create_token` is required by adapted frontends and enables response-loss retry.
- Gateway-owned Web creation overwrites any request-body `user_id` with the authenticated connection identity before forwarding to AgentServer.
- Initialization exceptions are logged and never publish READY.
- Continuous reconciles cancel superseded warm tasks; late old-revision completion cannot enter the pool.
- A READY root stays pinned until the claimed session reaches its first AgentServer request, preventing idle retirement in the create-to-send gap.
- Foreground entry cancels non-promoted background initialization. Cancellation remains cooperative, so a synchronous third-party call may finish before releasing the shared initialization lock, but no competing foreground initializer mutates process-global registries concurrently and no successor starts while chat is active.
- Runtime-state Git probes and Git-ignore checks execute in worker threads. Runtime-state writes are coalesced per adapter and globally bounded to two concurrent probes, so they do not synchronously hold the AgentServer loop.
- A lightweight/restored adapter without an initialized diagnostic-task slot treats that slot as idle, so optional runtime-state persistence cannot abort request configuration.

## Verification

- `tests/unit_tests/agentserver/test_agent_warm_pool.py` covers the default-on switch and its opt-out alongside global capacity, promotion/cancellation, code `sub_mode=normal`, work-mode canonicalization, and identical prewarm/chat cache identity.
- Lifecycle tests cover ownership, concurrency, binding, warm bypass, and TUI boot ordering: 114 Python tests passed in each prewarm state on 2026-08-03; TUI build, typecheck, and full tests passed.
- Adapter tests cover off-loop/coalesced runtime probes and per-workspace/config MemoryRail reindex singleflight.

## Known Gaps

The pool is process-local and does not preserve tokens across restart. External `clear.spec.ts` replay is pending; local coverage asserts pre-ack queuing and allocated first-chat identity. Live load validation, cancellation, cleanup, claim-pin expiry, and symbol audit remain pending.
