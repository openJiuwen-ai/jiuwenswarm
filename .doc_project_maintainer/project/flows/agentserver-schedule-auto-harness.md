---
id: agentserver-schedule-auto-harness
name: AgentServer Scheduled Auto-Harness
status: partial
confidence: confirmed
last_updated: 2026-07-13
user_visible_surface: "Web and TUI schedule creation, immediate runs, listing, status, logs, cancellation, deletion, and configuration."
source_of_truth:
  - "~/.jiuwenswarm/auto-harness/scheduled-tasks.json"
  - "~/.jiuwenswarm/auto-harness/runs/"
  - "~/.jiuwenswarm/auto-harness/config.yaml"
modules:
  - agentserver-runtime
  - agent-harness
  - gateway-and-channels
directories:
  - jiuwenswarm/server
  - jiuwenswarm/agents/harness/common/auto_harness
  - jiuwenswarm/gateway/channel_manager
code_symbols:
  - AgentWebSocketServer._handle_schedule_request
  - AgentWebSocketServer._resolve_model
  - AgentWebSocketServer._build_model_cache
  - AutoHarnessService.start_scheduler
  - AutoHarnessService.create_scheduled_task
  - AutoHarnessService.run_task
  - Scheduler._execute_scheduled_task
entrypoints:
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/gateway/channel_manager/web/app_web_handlers.py
  - jiuwenswarm/gateway/channel_manager/tui/tui_connect.py
---

# AgentServer Scheduled Auto-Harness

## Outcome

Web and TUI clients can manage recurring or one-time Auto-Harness work through `schedule.*` RPCs. AgentServer lazily starts a scheduler, persists task metadata, runs jobs through an Auto-Harness agent, stores structured logs, and returns task state to clients.

## Causal Path

Web/TUI methods `schedule.check_config`, `update_config`, `create`, `run`, `list`, `status`, `logs`, `cancel`, and `delete` cross the Gateway E2A connection and dispatch to `AgentWebSocketServer._handle_schedule_request`. The first request constructs one server-wide `AutoHarnessService` and starts its scheduler. Mutating actions resolve an Agent by request channel, mode, sub-mode, and project, then overwrite the service's current Agent. Create/run resolve a model from AgentServer's cache and write a task through `TaskStore`. `Scheduler` polls or receives an immediate trigger, builds a synthetic `AgentRequest`, streams `AutoHarnessService.run`, appends JSON events to the run log, and finalizes task status/history. List/status/log operations reconstruct visible progress from the task index and run logs.

## State Classification

- Source of truth: `scheduled-tasks.json`, per-execution logs below `runs/`, and Auto-Harness `config.yaml`.
- Runtime state: the single AgentServer `_scheduler_service`, its mutable `_agent`, scheduler loop/tasks, `TaskStore` cache, model cache, and active executions.
- Derived output: enriched task progress, log pages, validation results, and E2A response payloads.

## Replay, Restore, Or Reconstruction

On lazy startup, `TaskStore` reloads the task index and `start_scheduler` reconciles stale running statuses against terminal log evidence. The scheduler then resumes polling persisted pending tasks. Models are reconstructed from JiuwenSwarm config only when AgentServer's model cache is empty; the cache has no config fingerprint or reload invalidation. A failed first `start_scheduler` leaves `_scheduler_service` non-null, so a later request does not automatically retry initialization.

## Contract

The request method selects the action. Important fields include `query`, `interval_hours`, `run_immediately`, `model_name`, `pipeline`, `task_id`, `fields`, `log_type`, `history_index`, `offset`, and `limit`. Task records persist `task_id`, query, interval, timestamps, status, execution history, model name, and pipeline. They do not persist the originating channel, project directory, mode variant, or Agent cache identity.

## Consumer State And Output

Clients receive task IDs, next-run timestamps, task lists, enriched status, or log chunks. Scheduled execution uses a generated session/request identity and emits structured Harness events to disk. It currently hard-codes execution `channel_id="tui"`, independent of the request that created the task.

## Failure, Ordering, And Identity

The server-wide service contains one mutable `_agent`; any create/run/cancel/delete request can replace it with an Agent from another channel or project, while persisted tasks do not retain that identity. A later or concurrent request, or a restart followed by another request, can therefore run autonomous work against the wrong Agent/workspace. Scheduler startup occurs before Agent resolution and is not guarded by a visible initialization lock. Business errors returned as `payload.error` (including unknown/not-found cases) are still encoded with `ok=true`. Parameters are weakly typed and log pagination does not enforce a positive bounded limit, allowing read-all or oversized responses. Model-name-only caching also collapses same-name providers, can retain stale credentials, and may remain partially initialized after a constructor failure.

## Verification

Static evidence exists in `agent_ws_server.py`, `auto_harness/service.py`, `scheduler.py`, and `task_store.py`. Repository tests cover issue-runner and some Gateway scheduling-adjacent behavior, but no direct `_handle_schedule_request` RPC contract, cross-channel/project identity, scheduler initialization race, model-cache invalidation, malformed parameter, or response-size tests were found.

## Known Gaps

The intended ownership model for a scheduled task's channel, workspace, Agent variant, and credentials is not encoded in persisted data. Multi-process task-store coordination and the client interpretation of `ok=true` plus `payload.error` also need an explicit contract.
