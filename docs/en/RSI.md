# RSI optimization status and results

The iteration count in optimization details represents completed progress reported by the engine. A node for round N may appear before that round finishes. Local candidate attempts within a Harness round do not each advance the round count. An early discarded program candidate is also distinct from a completed evaluation.

Paper and Harness tasks display a pruning count when available. Explicit engine statistics take precedence; otherwise, the count comes from pruned tree nodes. Unknown counts are hidden. Rejected and pending nodes are excluded. Combination evaluations apply only to Harness tasks.

When a task completes, fails, terminates, or pauses, detail polling also updates the sidebar and stops the running indicator. The result summary uses the final snapshot so stale live progress cannot override it.

For failed tasks, click the icon beside the failure status to inspect the reason returned by the service. Node details also show rejection or pruning reasons. Historical tasks that only stored a generic reason cannot recover an unrecorded model error.
