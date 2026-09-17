---
symbol: AgentWebSocketServer._handle_sandbox_files_remove
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_sandbox_files_remove(self, channel_id: str, params: dict[str, Any]) -> dict[str, Any]"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: clear
  output_contract: implicit
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: low
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
    dimension: state_mutation
    severity: high
    status: open
    summary: "Concurrent sandbox file mutations can lose updates."
    evidence: "The server creates one task per frame. This method reads, edits, and replaces the whole files mapping without a lock/version check, so concurrent mutations can persist a stale snapshot last."
    suggested_action: "Serialize mutations or use atomic locked read-modify-write/CAS."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Persistence can succeed while the active sandbox keeps the old policy."
    evidence: "Lines 4932-4933 persist before apply. The wrapper swallows adapter errors, and remote force-recreate failure only warns, so this method still returns success."
    suggested_action: "Propagate an applied/degraded result and roll back or explicitly queue reconciliation when remote recreation fails."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Auto-managed path protection can use incomplete project context."
    evidence: "When no adapter exists, _resolve_active_project_dir returns None before checking params.cwd/trusted_dirs. find_auto_managed_match can then miss the requested project path and permit removal from persisted user entries even though policy construction will auto-add it later."
    suggested_action: "Resolve request fallbacks without an adapter and reuse one context for match, dry-run, and apply."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Sandbox file removal has no direct regression coverage."
    evidence: "No test references command.sandbox files.remove, this helper, its path matching, dry-run-before-persist ordering, concurrent mutation, or patch failure."
    suggested_action: "Cover canonical and legacy paths, both buckets, auto-managed rejection, missing entries, dry-run failure, concurrency, and degraded apply."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_sandbox_files_remove`

## Actual Role

Validates a `files.remove` request, canonicalizes its host path, rejects auto-managed paths, removes every equivalent legacy or structured entry from both allow and deny buckets, dry-runs the resulting filesystem policy, persists the complete files mapping, and asks the active adapter to recreate its remote sandbox policy.

## Key Signals

- Input: Channel ID plus one path and optional routing/project context; unexpected params are rejected.
- Output: Updated runtime mapping, or `ValueError` for invalid, protected, absent, or unbuildable paths.
- Side effects: Reads and writes global sandbox config, inspects host paths, mutates adapter policy cache, and may recreate a jiuwenbox sandbox.
- Main risk: The read-modify-write and persist-then-apply sequence is neither serialized nor transactional.
- Tests/flow: No direct sandbox-files tests were found, and project docs contain no dedicated sandbox flow.

## Detail Index

- Detail docs pending.
