---
symbol: AgentWebSocketServer._apply_sandbox_runtime_patch
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_apply_sandbox_runtime_patch(self, channel_id: str, runtime: dict[str, Any], *, files_changed: bool) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: implicit
  output_contract: weak
  side_effects: explicit
  error_handling: flawed
  state_mutation: shared
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A global runtime change is patched into at most one arbitrary cached agent."
    evidence: "get_agent_nowait(channel_id) is unqualified by mode, sub-mode, or project and returns a preferred/first cached agent. Other live adapters on the channel, and adapters on other channels, keep their previous sandbox policy."
    suggested_action: "Fan out global patches to every active sandbox adapter and aggregate per-instance applied/skipped/failed results."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "None conflates applied, skipped, and failed outcomes."
    evidence: "Missing agent/adapter returns silently and ordinary adapter exceptions are logged then swallowed. The Deep adapter also returns after no active card/missing launcher/import failure and swallows remote sandbox recreation failure."
    suggested_action: "Return a structured application result and require callers to expose degraded or pending state instead of unconditional success."
  - id: ISSUE-003
    dimension: state_mutation
    severity: high
    status: open
    summary: "Callers commit config before an unverifiable live patch."
    evidence: "All four callers invoke update_sandbox_runtime before awaiting this helper. Any skip/failure leaves persisted config ahead of live adapters; files_changed may also mutate the card policy before remote recreation fails, with no rollback."
    suggested_action: "Use a transactional apply/commit protocol or retain prior config/card state and roll back failed targets."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No runtime-patch helper or command integration tests were found."
    evidence: "No tests cover absent/multiple agents, skipped adapters, exception classes, remote recreate failure, partial mutation, fanout, rollback, or response status."
    suggested_action: "Add fake multi-adapter tests and command-level persistence-versus-live consistency cases."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._apply_sandbox_runtime_patch`

## Actual Role

Selects one cached channel agent and asks its adapter to update sandbox runtime; file changes may rebuild jiuwenbox. It returns no status, treats several skips/failures as success, and runs only after global config persistence.

## Key Signals

- Input: Channel id, complete sandbox runtime, and `files_changed` recreation flag.
- Output: None; `ValueError` only for adapter `FileNotFoundError`/`ValueError`.
- Side effects: May mutate one adapter card and recreate one remote sandbox.
- Main risk: Persisted, cached, and remotely active policy can diverge while the command reports success.
- Tests/flow: No direct or adjacent tests found; no sandbox patch-application flow exists.

## Detail Index

- Detail docs pending.
