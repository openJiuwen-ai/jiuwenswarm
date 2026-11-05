# Worker interruption and Team recovery boundaries

A ready worker can accept requests; it does not prove that an old Team resumed. A model's chat.final is not product completion, and completed tasks are not independent product acceptance.

## Read-only diagnosis

Query team.list and team.snapshot with the same session_id/team_name. Snapshot fields are additive:

| Field | Meaning |
|---|---|
| members_source | Existing board source: live, live-scoped, db or empty |
| members_live | The board came from a live monitor, not that every member is working |
| runtime_available | A matching Team monitor exists; null on probe failure or unresolved identity |
| recovery_required | True when the runtime is confirmed absent and the historical board has unfinished tasks; null when availability is unknown or the board is missing |

Persisted busy/running rows remain unchanged historical evidence, not proof of live execution. A live runtime with a DB board fallback can have runtime_available=true and members_live=false. recovery_required neither identifies a crash cause nor promises recoverability or initiates replay.

## Investigating a failure

1. Inspect a bounded window from the last activity to the first missing observation for Supervisor warnings, worker PID/instance, exits and replacements. These events often have no session_id.
2. Distinguish transport heartbeats, business-loop beats, gateway backlog and Supervisor loop lag. A listening port or replacement-worker readiness does not prove Team continuity.
3. Read bounded tails of the associated stderr/dumps. Large logs, overflow and one stack sample are clues, not exclusive root-cause proof. Pool exhaustion requires connection ownership, checkout/checkin and cancellation investigation, not longer heartbeat deadlines.
4. The sink fix removes synchronous per-record overflow logging while holding the producer state lock. Ingress only counts losses; the writer aggregates warnings every 30 seconds and reports remaining losses on close. Exact dropped/dropped_final/dropped_frames counters remain available, queues stay bounded, and no unbounded logging queue is added. Slow logging may stall the writer but must not stall ingress.

## Recovery and verification

Preserve worktrees, uncommitted changes, task storage and checkpoints. Before explicit recovery, reconcile tools whose side effects are unknown, especially external writes. Do not blindly replay the original request or duplicate its tasks. Use the normal recovery entry point for the same identity and require team.recovered, live runtime/member evidence and new engineering activity before claiming resumed execution.

This patch does not implement automatic task replay after a crash or establish long-run reliability. Unit tests cover slow-logging isolation, loss accounting and snapshot provenance. Release validation still needs isolated worker-fault injection, same-identity recovery and a long-running multi-member test; do not restart a real experiment without authorization.

## Isolated preflight and remaining gaps

Run these tests from the repository root with the platform environment's Python. No real model credentials are required:

```bash
python -m pytest tests/unit_tests/agentserver/test_team_reliability_preflight.py tests/unit_tests/agentserver/test_team_snapshot.py tests/unit_tests/agentserver/test_worker_loop_liveness.py tests/unit_tests/observability/test_trace_sink.py --no-cov -q
```

The checks import the installed agent-core directly. Record both platform and SDK full revisions; results from another checkout do not establish the running environment's behavior.

- **Slow logging:** with the writer's warning blocked, the async producer submits 2,500 frames and advances a heartbeat between batches. This covers the overflow path, not every logging handler or disk failure.
- **Reader cleanup:** a temporary file-backed SQLite pool with two reader slots recovers after ordinary cancellation and watchdog expiry. Ten bursts of 32 readers also return their slots. This does not reproduce the historical leak's root cause.
- **Native driver stalls:** when a controlled SQLite function blocks, cleanup still waits for its return after the Python watchdog expires. The watchdog is not a hard deadline that terminates the driver thread.
- **Cold pool versus persistence:** replacing an in-process runtime pool while preserving a real temporary Team DB leaves in_progress rows but does not make `restore_resumable_runtime` rebuild a Team. This simulates pool replacement, not a real Worker kill/restart, cold recovery, or replay of external effects.
- **Completion:** a deterministic model stub returns PARTIAL through the real ReAct/TaskLoop; ordinary invoke does not implicitly create a Goal. An explicit Goal with an output consumer and a continue assessment schedules a second attempt. A separate controlled-board probe uses the real Team completion predicate: pending/in_progress blocks completion, but terminal completed/cancelled tasks with settled members and read messages can complete even when product requirements were never mapped onto that board. A PARTIAL answer does not itself immediately kill the entire Team.

Do not bind every member to the same unlimited Goal. Member responsibilities, Leader coordination waits, team acceptance, output leases and recovery identity need distinct handling. Define a general persistent engineering-goal contract first: outstanding requirements survive a drained board; success references a fixed candidate and acceptance evidence; blocked work, exhausted budgets, user stops and successful delivery remain distinct outcomes.

Until this contract and real Worker fault-injection checks pass, unattended full-scale long-running experiments remain **NOT READY**. Passing these regressions alone does not close recovery or premature-conclusion issues.
