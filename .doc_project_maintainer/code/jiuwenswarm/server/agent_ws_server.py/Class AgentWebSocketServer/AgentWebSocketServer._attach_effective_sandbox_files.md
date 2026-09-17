---
symbol: AgentWebSocketServer._attach_effective_sandbox_files
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_attach_effective_sandbox_files(self, payload: dict[str, Any], channel_id: str, params: dict[str, Any] | None = None) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: implicit
  output_contract: weak
  side_effects: hidden
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
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A project-mismatched adapter policy is still returned as effective."
    evidence: "When adapter._project_dir differs from the resolved request project, the method only logs a warning. It then reads that adapter's cached policy, assigns it to effective_files, and returns."
    suggested_action: "Reject mismatched adapters and build from the requested project, or return an explicit identity-mismatch/degraded result."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "Cached policy is not proof of the policy active in jiuwenbox."
    evidence: "apply_sandbox_runtime_patch mutates the card policy before force_recreate_jiuwenbox_sandbox; recreate import/runtime failures are swallowed. This method later presents that mutated cache as effective without an applied generation or remote confirmation."
    suggested_action: "Track applied sandbox generation/id and expose configured versus confirmed-active policy separately."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "Any enrichment failure silently weakens a successful response."
    evidence: "The outer broad except only logs a warning and leaves effective_files absent. _handle_command_sandbox still returns ok=true with no degraded marker or stable field shape."
    suggested_action: "Always attach a structured effective_files status, including source and error/degraded state, or fail the command when accuracy is required."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No effective-files attachment tests were found."
    evidence: "No tests cover cached versus fallback sources, project mismatch, stale/recreate-failed cards, malformed payloads, lookup failures, field omission, or host-directory side effects."
    suggested_action: "Add focused helper and command.sandbox response tests with fake adapters and isolated filesystem/config state."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._attach_effective_sandbox_files`

## Actual Role

Adds an `effective_files` view to successful `/sandbox` payloads. It prefers a selected adapter's cached policy, otherwise derives a view from payload/config plus auto-managed paths, and suppresses failures. The fallback may create the agent workspace directory.

## Key Signals

- Input: Mutable response payload, channel id, and optional project metadata.
- Output: None; normally adds `payload["effective_files"]`, but can leave it absent.
- Side effects: Mutates payload, reads runtime/adapter state, and may create a workspace directory.
- Main risk: The reported write boundary can belong to another project or to a cache not confirmed active remotely.
- Tests/flow: No direct or adjacent tests found; no sandbox effective-policy flow exists.

## Detail Index

- Detail docs pending.
