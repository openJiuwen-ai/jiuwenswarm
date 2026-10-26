---
symbol: AgentWebSocketServer._handle_sandbox_exclude_remove
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_sandbox_exclude_remove(self, channel_id: str, params: dict[str, Any]) -> dict[str, Any]"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
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
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "Persistence commits before best-effort live enforcement."
    evidence: "update_sandbox_runtime removes the exclusion before _apply_sandbox_runtime_patch. The patch helper silently returns for no adapter and swallows ordinary exceptions; ValueError/FileNotFoundError propagate only after YAML changed, with no rollback."
    suggested_action: "Validate and stage the live patch before commit, then rollback or return an explicit persisted-but-not-applied state on failure."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A successful remove can leave other active agents using the old bypass list."
    evidence: "The live-patch helper calls get_agent_nowait(channel_id) without mode/project and updates at most the first matching adapter. Other cached modes/projects on the channel retain the removed exclusion until recreation."
    suggested_action: "Fan out the patch to every active sandbox adapter and aggregate per-runtime results before reporting success."
  - id: ISSUE-003
    dimension: input_contract
    severity: medium
    status: open
    summary: "Pattern normalization is inconsistent with persisted runtime normalization."
    evidence: "Request patterns are str-converted and stripped, while get/update_sandbox_runtime preserve whitespace in stored entries. A legacy/manual value such as ' git ' cannot be removed through this command using either spelling."
    suggested_action: "Require string patterns and centrally store/compare one canonical representation."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No exclude-remove command or helper tests were found."
    evidence: "There is no coverage for missing/duplicate/canonical patterns, persistence failure, absent or multiple adapters, live-patch failure, response codes, or persisted-versus-live consistency."
    suggested_action: "Add direct helper and command.sandbox tests with temp config and fake multi-adapter patch outcomes."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_sandbox_exclude_remove`

## Actual Role

Implements `/sandbox exclude remove`: validates an exact pattern, removes every equal entry from the persisted exclusion list, asks one selected active adapter to hot-patch its sandbox card, and returns the complete runtime. The parent command adds effective-file/Landlock status and maps `ValueError` to `SANDBOX_BAD_REQUEST`.

## Key Signals

- Input: Channel id and params containing a non-empty `pattern`.
- Output: Updated runtime, or `ValueError` when the pattern is absent/not found.
- Side effects: Rewrites all sandbox runtime fields in config and may mutate one active adapter's launcher parameters.
- Main risk: The security-tightening removal can be persisted and reported successful while live runtimes continue excluding the command.
- Tests/flow: No direct or command-level tests found. Project plans identify sandbox runtime mutation as pending, but no sandbox flow document exists.

## Detail Index

- Detail docs pending.
