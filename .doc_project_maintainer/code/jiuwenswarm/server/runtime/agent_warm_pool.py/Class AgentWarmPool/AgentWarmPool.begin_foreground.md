---
symbol: AgentWarmPool.begin_foreground
kind: method
source: jiuwenswarm/server/runtime/agent_warm_pool.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWarmPool
signature: "begin_foreground(self) -> None"
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

# AgentWarmPool.begin_foreground

## Actual Role

Atomically increments the active foreground-chat count, closes the foreground-idle gate, cancels every non-promoted speculative initialization task, and yields once so cooperative cancellation is delivered before foreground preparation contends for the shared initialization lock.

## Key Signals

- Input: none.
- Output: none.
- Main side effects: mutates process-local scheduling state, cancels speculative tasks, requeues their keys, and emits a pause log.
- Main risk: callers must pair it with `end_foreground` in `finally`.
- Related tests: `test_foreground_bypasses_background_and_pauses_lazy_dispatch`, `test_ws_send.py` foreground-manager assertion.

## Detail Index

- Detail docs pending.
