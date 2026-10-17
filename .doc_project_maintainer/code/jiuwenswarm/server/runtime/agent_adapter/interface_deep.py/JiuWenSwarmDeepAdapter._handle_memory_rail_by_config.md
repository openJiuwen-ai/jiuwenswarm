---
symbol: JiuWenSwarmDeepAdapter._handle_memory_rail_by_config
kind: method
source: jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
source_role: runtime_source
audit_scope: default_health_audit
class: JiuWenSwarmDeepAdapter
signature: "_handle_memory_rail_by_config(self, mode: str)"
health:
  overall: unknown
audit:
  status: unaudited
confidence: confirmed
---

# JiuWenSwarmDeepAdapter._handle_memory_rail_by_config

## Actual Role

Applies MemoryRail configuration to the session adapter and compares embedding fingerprints. First registration records the fingerprint without forcing a repository-wide reindex; an actual change from a previous fingerprint delegates to the singleflight reindex scheduler.

When the request-scoped eternal-conversation flag is active, the caller bypasses this legacy semantic-memory path so two systems cannot independently summarize the same Session. Eternal memory publication remains exclusively owned by its Session coordinator and dynamic-memory-cli state.

## Key Signals

- Input: runtime mode and configured memory/embedding state.
- Output: asynchronous configuration completion.
- Main side effects: registers MemoryRail state and may schedule one full reindex for a changed configuration.
- Main risk: incorrect fingerprinting could skip or over-trigger reindexing.
- Related test: `test_memory_reindex_is_singleflight_per_workspace_and_config`.

Health remains unaudited; see the build plan for the pending audit slice.
