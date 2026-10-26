---
symbol: AgentWebSocketServer._handle_sandbox_enable
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_sandbox_enable(self, channel_id: str) -> dict[str, Any]"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: overloaded
  length: long
  complexity: high
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: implicit
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
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Malformed or unix URLs silently target the default TCP endpoint."
    evidence: "_parse_sandbox_host_port falls back to 127.0.0.1:8321; when the port is unchanged, the original unsupported URL is still persisted while the runner uses TCP."
    suggested_action: "Require a supported scheme, host, and port; reject unix:// until implemented."
  - id: ISSUE-002
    dimension: input_contract
    severity: medium
    status: open
    summary: "External mode unnecessarily requires a local policy file."
    evidence: "Policy checks precede the mode branch, although JiuwenBoxRunner only health-checks external servers and documents policy_path as internal-only."
    suggested_action: "Require local policy existence only for internal startup."
  - id: ISSUE-003
    dimension: state_mutation
    severity: high
    status: open
    summary: "Post-start enablement is non-transactional and can leave partial state."
    evidence: "The runner may be started and runtime.enabled is persisted before recreate_agent. A channel with no active agents makes recreate_agent log then dereference None, so the command fails after enablement; later failures have no rollback."
    suggested_action: "Stage and commit endpoint/runtime/agent changes transactionally, or compensate process and config state on failure."
  - id: ISSUE-004
    dimension: output_contract
    severity: high
    status: open
    summary: "Success metadata can claim recreation despite failed agent modes or endpoint persistence."
    evidence: "Endpoint write errors are swallowed; recreate_agent also swallows per-mode _create_agent errors, yet this method unconditionally returns agent_recreated=True and ready=True."
    suggested_action: "Require verified endpoint persistence and an aggregate recreation result before returning success."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No enable-path tests were found."
    evidence: "There is no direct helper or command.sandbox enable coverage for URL parsing, policy modes, runner failure, persistence, no-agent recreation, partial rebuild, or rollback."
    suggested_action: "Use fake runner/manager and temp config for success and post-start failures."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_sandbox_enable`

## Actual Role

Runs `/sandbox enable`: resolves endpoint/policy, starts or probes JiuwenBox, persists endpoint and enabled runtime, then recreates the channel's agents. The caller adds effective-file and Landlock status.

## Key Signals

- Input: Channel id whose agents should adopt the enabled runtime.
- Output: Runtime, endpoint, readiness, and recreation claims.
- Side effects: Starts/probes JiuwenBox, writes shared config twice, and drops/rebuilds channel agents.
- Main risk: Non-transactional steps can leave a running process or enabled config after an error, while success fields overstate results.
- Tests/flows: No enable-path tests or dedicated sandbox-enable flow document found.

## Detail Index

- Detail docs pending.
