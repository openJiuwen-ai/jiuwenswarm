---
symbol: JiuWenSwarmDeepAdapter._schedule_memory_reindex
kind: method
source: jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
source_role: runtime_source
audit_scope: default_health_audit
class: JiuWenSwarmDeepAdapter
signature: "_schedule_memory_reindex(self) -> None"
health:
  overall: unknown
audit:
  status: unaudited
confidence: confirmed
---

# JiuWenSwarmDeepAdapter._schedule_memory_reindex

## Actual Role

Schedules a full MemoryRail reindex only when no task with the same normalized workspace and embedding fingerprint is already in flight. The singleflight key is released in the worker's `finally` path so later real configuration changes or retries remain possible.

The scheduler itself is unchanged, but eternal-conversation requests do not enter the legacy MemoryRail configuration/reindex path.

## Key Signals

- Input: adapter workspace and current embedding configuration.
- Output: none; work is submitted asynchronously.
- Main side effects: mutates the process-wide in-flight key set and schedules the memory reindex worker.
- Main risk: the underlying indexer may still be expensive, but duplicate session initialization no longer multiplies identical work.
- Related test: `test_memory_reindex_is_singleflight_per_workspace_and_config`.

Health remains unaudited; see the build plan for the pending audit slice.
