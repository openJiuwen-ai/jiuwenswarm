---
symbol: AgentWebSocketServer._get_stateless_agent
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_get_stateless_agent(channel_id: str) -> Any"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: partial
  input_contract: clear
  output_contract: weak
  side_effects: hidden
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: missing
  observability: missing
  performance_risk: medium
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  audited_symbol_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "The no-adapter-rebuild guarantee does not hold for mutation routes."
    evidence: "Downstream skills/plugins handlers call await self.create_instance() after install/toggle/reload; a cached project-scoped agent may therefore rebuild an unrelated adapter."
    suggested_action: "Separate stateless RPC services from JiuWenSwarm adapter lifecycle and test all mutation routes."
  - id: ISSUE-002
    dimension: state_mutation
    severity: high
    status: open
    summary: "Fallback instances lose asynchronous SkillNet job continuity."
    evidence: "Each cache miss creates a new SkillManager, while install jobs live in _skillnet_install_jobs; the next install_status request can receive a fresh empty map."
    suggested_action: "Use one durable stateless service per server/channel or persist job state independently."
  - id: ISSUE-003
    dimension: side_effects
    severity: medium
    status: open
    summary: "A cache miss performs hidden filesystem work on every request."
    evidence: "JiuWenSwarm() constructs SkillManager, which mkdirs the skills directory, loads state, and scans/registers unmanaged skills."
    suggested_action: "Cache a dedicated lightweight stateless service with explicit lifecycle and invalidation."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No direct tests cover cache-hit/miss lifecycle semantics."
    evidence: "No test calls _get_stateless_agent or proves adapter non-rebuild, SkillNet status continuity, concurrent fallback behavior, or project-scoped cache isolation."
    suggested_action: "Add focused unary/stream tests for hit, miss, mutation, async status, and concurrency paths."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._get_stateless_agent`

## Actual Role

Returns the first cached channel agent whose mode is `agent`; otherwise constructs an uncached `JiuWenSwarm` wrapper. Unary and streaming stateless RPC paths use the result to bypass normal mode resolution, but downstream mutation handlers can still create/rebuild adapters and instance-local services.

## Key Signals

- Input: Channel id used by `AgentManager.get_agent_nowait`.
- Output: A cached shared or new uncached `JiuWenSwarm`; the `Any` contract does not expose which.
- Main side effects: Cache misses initialize SkillManager filesystem/state; returned wrappers may later rebuild adapters or hold async jobs.
- Main risk: Behavior and state continuity depend on unrelated cache residency.
- Related tests: Downstream skill/Symphony tests exist; no direct stateless-agent lifecycle test was found.

## Detail Index

- Detail docs pending.
