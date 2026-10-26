---
symbol: AgentWebSocketServer._dry_run_files_policy
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_dry_run_files_policy(self, channel_id: str, params: dict[str, Any], files: dict[str, Any]) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: clear
  state_mutation: none
  dependency_coupling: high
  test_coverage: missing
  observability: not_applicable
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
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Dry-run can validate against a fallback project instead of the request context."
    evidence: "_resolve_active_project_dir returns None immediately when no adapter exists, before considering params.cwd or trusted_dirs. build_filesystem_policy then resolves project_dir=None through JIUSWARM_SANDBOX_PROJECT_DIR or process cwd, so a pre-agent files change may be checked against a different project mount set."
    suggested_action: "Honor cwd/trusted_dirs without requiring an active adapter and reject an explicitly supplied but invalid project context instead of silently falling back."
  - id: ISSUE-002
    dimension: test_coverage
    severity: high
    status: open
    summary: "The write-before-persist policy gate has no regression coverage."
    evidence: "No test references _dry_run_files_policy or build_filesystem_policy, and no command.sandbox files allow/deny/remove handler test was found."
    suggested_action: "Cover valid and missing paths, malformed runtime, startup modes, no-agent cwd/trusted_dirs fallback, and proof that failed dry-runs do not persist."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._dry_run_files_policy`

## Actual Role

Builds and discards the prospective jiuwenbox filesystem policy before sandbox file changes are persisted. It derives project and agent context, passes the candidate allow/deny runtime plus current startup mode to `build_filesystem_policy`, and converts missing host paths from `FileNotFoundError` into `ValueError` so the outer command handler returns `SANDBOX_BAD_REQUEST` rather than writing an unusable configuration.

## Key Signals

- Input: Channel ID, command params carrying project context, and the complete candidate `sandbox.files` mapping.
- Output: `None` on success; schema `ValueError` propagates and missing-path `FileNotFoundError` is normalized to `ValueError`.
- Side effects: No persistence or policy installation; callees inspect host paths and global startup configuration.
- Call chain: `_handle_sandbox_files_set` and `_handle_sandbox_files_remove` call it immediately before `update_sandbox_runtime`.
- Context caveat: The current builder ignores `is_code_agent`; project resolution still controls the automatically writable project bind.
- Tests/flow: No direct wrapper, policy-builder, or sandbox-files command test was found, and project docs have no dedicated sandbox flow.

## Detail Index

- Detail docs pending.
