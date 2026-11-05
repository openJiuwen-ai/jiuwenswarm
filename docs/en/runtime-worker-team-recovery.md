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
