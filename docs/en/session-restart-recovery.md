# Timing, concurrency and cancellation boundaries of session recovery

After a Front loop stall, frame freshness uses the IPC decode time. Draining queued pongs does not refresh it. A recent request completion received before probe registration can prove liveness once, only on the same connection and generation after matching an actually issued fresh request. At most 256 correlation metadata entries are retained, without business payloads. Normal heartbeat and business-loop stall thresholds remain unchanged.

History prechecks before recovery admission acquire locks without waiting. A busy lock defers to the bounded recovery path, rather than occupying shared I/O threads. Recovery concurrency and its waiting queue remain bounded.

Repeated terminal receipts and automatic compression audit rows do not count as later model activity. Real late tool results still stay with their original calls, and genuinely oversized tool groups still reject recovery. Original history is neither rewritten nor truncated.

Cancellation during unarchive registration also marks an already persisted operation failed and wakes recovery. Execution ownership remains held until accepted file operations finish. Failure marking only affects the matching operation_id.

A ready Worker does not prove session recovery. Acceptance also checks old history in new inference, durable new turns, preserved team members and tasks, and process cleanup. Continued session use does not authorize automatic replay of external tool side effects interrupted by a crash.
