---
symbol: AgentWebSocketServer._handle_sandbox_exclude_add
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_sandbox_exclude_add(self, channel_id: str, params: dict[str, Any]) -> dict[str, Any]"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: clear
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
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "Persistence commits before best-effort live enforcement."
    evidence: "update_sandbox_runtime writes config before _apply_sandbox_runtime_patch. The patch helper silently returns when no adapter exists and swallows ordinary exceptions; ValueError/FileNotFoundError propagate only after YAML changed, with no rollback."
    suggested_action: "Validate and stage the live patch before commit, then rollback or return an explicit persisted-but-not-applied state on failure."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Unrestricted exclusions can broadly bypass sandbox execution."
    evidence: "The method accepts any non-empty string, including catch-all patterns such as '*', and persists it as excluded_commands. The command UI defines a match as running locally instead of in the sandbox; there is no breadth guard, confirmation, or policy limit."
    suggested_action: "Reject or require explicit elevated confirmation for catch-all/broad patterns, and document bounded exclusion semantics."
  - id: ISSUE-003
    dimension: input_contract
    severity: medium
    status: open
    summary: "Pattern validation relies on lossy string coercion and exact duplicate matching."
    evidence: "str(...).strip() turns non-string JSON values into patterns. Duplicate detection is exact, with no glob validation or canonicalization."
    suggested_action: "Require a string, validate supported pattern syntax and limits, and compare a centrally defined canonical representation."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No exclude-add helper or command tests were found."
    evidence: "No tests cover missing/duplicate/catch-all patterns, persistence failure, absent adapters, hot-patch failure, response mapping, or persisted-versus-live consistency."
    suggested_action: "Add direct helper and command.sandbox tests using temporary config and fake adapter patch outcomes."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_sandbox_exclude_add`

## Actual Role

Implements `/sandbox exclude add`: validates a non-empty unique pattern, persists it in sandbox `excluded_commands`, hot-patches one selected channel adapter, and returns the runtime. The parent maps `ValueError` to `SANDBOX_BAD_REQUEST` and adds effective-file and Landlock status.

## Key Signals

- Input: Channel id and params containing `pattern`.
- Output: Updated runtime, or `ValueError` for a missing or exact duplicate pattern.
- Side effects: Rewrites sandbox runtime config and may mutate one active adapter's launcher parameters.
- Main risk: A broad pattern can move matching commands outside sandbox isolation, while persisted and live policy can diverge after patch failure or omission.
- Tests/flow: No direct or command-level tests found, and no sandbox flow document describes exclusion mutation.

## Detail Index

- Detail docs pending.
