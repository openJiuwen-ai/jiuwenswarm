---
symbol: AgentWarmPool.end_foreground
kind: method
source: jiuwenswarm/server/runtime/agent_warm_pool.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWarmPool
signature: "end_foreground(self) -> None"
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

# AgentWarmPool.end_foreground

## Actual Role

Atomically decrements the foreground count and, when the final chat leaves, opens the idle gate and schedules delayed lazy background replenishment.

## Key Signals

- Input: none.
- Output: none.
- Main side effects: mutates scheduling state and may create one delayed pump task.
- Main risk: unmatched calls could resume background work too early; the count is clamped at zero.
- Related tests: `test_foreground_bypasses_background_and_pauses_lazy_dispatch`.

## Detail Index

- Detail docs pending.
