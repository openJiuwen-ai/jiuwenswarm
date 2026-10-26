---
symbol: AgentWebSocketServer._handle_sandbox_files_set
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_sandbox_files_set(self, channel_id: str, params: dict[str, Any], *, bucket: str) -> dict[str, Any]"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: partial
  output_contract: weak
  side_effects: explicit
  error_handling: partial
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
    dimension: state_mutation
    severity: high
    status: open
    summary: "Config commits before live recreation succeeds."
    evidence: "Files persist before hot-patch/recreate. Missing adapters and ordinary failures are swallowed; validation errors can propagate after YAML changed. There is no rollback/degraded result."
    suggested_action: "Stage sandbox creation before commit, then publish atomically or compensate all state."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Validation and application can target different runtime scopes."
    evidence: "Params affect dry-run project resolution, but live patch rebuilds from one get_agent_nowait(channel_id) adapter. Other modes/projects stay stale; no-agent cwd/trusted_dirs can fall back elsewhere."
    suggested_action: "Use one project identity and patch every affected adapter with aggregate results."
  - id: ISSUE-003
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Nested conflicts with auto-managed paths are not rejected."
    evidence: "Auto-managed matching checks equality only; nested checks see user files only. A deny ancestor of the auto-managed writable project can pass despite the unsupported deny-parent/allow-child shape."
    suggested_action: "Include auto-managed entries in the same exact and ancestor/descendant conflict graph before dry-run."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No sandbox files-set tests were found."
    evidence: "No coverage exists for success, canonicalization, conflicts, invalid inputs, dry-run/persistence, multi-adapter patch, recreate failure, or rollback."
    suggested_action: "Add temp-filesystem/config and fake-adapter tests at helper and command layers."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_sandbox_files_set`

## Actual Role

Implements `/sandbox files allow|deny`: canonicalizes a path, blocks selected conflicts, dry-runs policy, persists files, then asks one adapter to rebuild policy and recreate its sandbox.

## Key Signals

- Input: Channel, params with `path`/project hints, and caller-supplied `allow` or `deny` bucket.
- Output: Complete updated runtime, or `ValueError` for rejected input/policy.
- Side effects: Resolves/stats host paths, rewrites sandbox config, mutates an adapter card, and may recreate a remote sandbox.
- Main risk: A security policy can be persisted and reported while active sandboxes remain stale or were validated under a different project context.
- Tests/flow: No helper/command tests or dedicated sandbox flow found; project build plan lists sandbox policy mutation as pending.

## Detail Index

- Detail docs pending.
