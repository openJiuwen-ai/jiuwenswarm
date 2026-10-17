---
symbol: JiuWenSwarmDeepAdapter._schedule_runtime_state_write
kind: method
source: jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
source_role: runtime_source
audit_scope: default_health_audit
class: JiuWenSwarmDeepAdapter
signature: "_schedule_runtime_state_write(self, *, mode, language, channel, session_id, project_dir) -> None"
health:
  overall: unknown
  name_behavior_match: unknown
  responsibility_focus: unknown
  length: unknown
  complexity: unknown
  implementation_soundness: unknown
  boundary_safety: unknown
  input_contract: unknown
  output_contract: unknown
  side_effects: unknown
  error_handling: unknown
  state_mutation: unknown
  dependency_coupling: unknown
  test_coverage: unknown
  observability: unknown
  performance_risk: unknown
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  audited_symbol_hash: null
  confidence: unknown
  expired_reason: null
issues: []
confidence: confirmed
details: {}
---

# JiuWenSwarmDeepAdapter._schedule_runtime_state_write

## Actual Role

Coalesces diagnostic runtime-state persistence to at most one active task per adapter and runs synchronous Git/file probing in a worker thread under a process-wide bound of two writers. It returns without awaiting that diagnostic work. Lightweight or restored adapters that bypassed `__init__` treat a missing task slot as idle rather than failing request configuration.

The eternal-conversation request flag is intentionally not persisted here in V1; the frontend must explicitly send it, while durable memory/cursor state lives under the Session feature root.

## Key Signals

- Input: stable/request runtime identity and project context.
- Output: none.
- Main side effects: schedules a named task that writes a per-session YAML runtime-state file.
- Main risk: a request arriving during an existing write is intentionally coalesced rather than persisted separately.
- Related tests: `test_runtime_state_git_probe_is_non_blocking_and_coalesced` and `test_runtime_config_syncs_channel_and_task_workspace`.

## Detail Index

- Detail docs pending.
