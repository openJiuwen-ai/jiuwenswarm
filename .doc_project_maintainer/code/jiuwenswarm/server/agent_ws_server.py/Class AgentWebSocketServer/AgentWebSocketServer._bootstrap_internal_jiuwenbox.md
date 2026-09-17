---
symbol: AgentWebSocketServer._bootstrap_internal_jiuwenbox
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_bootstrap_internal_jiuwenbox(self) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: missing
  observability: clear
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
    severity: medium
    status: open
    summary: "Unrelated sandbox endpoint validation can suppress auto-start."
    evidence: "The method calls get_sandbox_endpoint() for URL/type/policy data; that helper also validates preserve_file_sharing_mode, so an unrelated invalid field is caught by the broad exception path and skips auto-start."
    suggested_action: "Read only boot-required sandbox fields here, or isolate preserve_file_sharing_mode validation from startup bootstrap."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Direct jiuwenbox bootstrap behavior is untested."
    evidence: "Targeted searches found no unit tests for non-Linux skip, explicit-mode decisions, policy-missing skip, runner failure, successful endpoint persistence, or runtime enabled persistence."
    suggested_action: "Add async unit tests with monkeypatched platform, config, policy resolver, runner, and config write functions."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: low
    status: open
    summary: "WebSocket listener opens before sandbox bootstrap has settled."
    evidence: "start() binds the WebSocket server before awaiting this method; a Gateway could connect before the effective sandbox URL is persisted after a successful port change."
    suggested_action: "Gate request handling until bootstrap finishes, or document and test that Gateway connections occur only after the app-level ready point."
  - id: ISSUE-004
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Persistence failures can leave enabled state paired with a stale endpoint."
    evidence: "After ensure_running succeeds, endpoint-write errors are logged but enabled=True is still written; after port reassignment, later agents can read the old endpoint."
    suggested_action: "Persist both values atomically, or do not enable runtime when endpoint persistence fails."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._bootstrap_internal_jiuwenbox`

## Actual Role

Best-effort startup hook that, only on Linux with explicit `sandbox.startup_mode: internal`, resolves endpoint and policy, allocates a usable port, and awaits `JiuwenBoxRunner.ensure_running`. After success it separately persists the effective endpoint and `sandbox.enabled=True`; failures are logged and suppressed so AgentServer startup continues.

## Key Signals

- Input: process platform and sandbox configuration from `config.yaml`.
- Output: no return value; startup success or skip/failure is communicated through logs and persisted config updates.
- Main side effects: may spawn a jiuwenbox subprocess, mutate sandbox endpoint/runtime config, and log warnings for best-effort failures.
- Main risk: broad config dependency, inline process startup, and independent persistence writes can expose delayed readiness or partially updated configuration.
- Related tests: no direct tests found; pending flow slice `agentserver-sandbox-runtime` should cover this path.

## Detail Index

- Detail docs pending.
