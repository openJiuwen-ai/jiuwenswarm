---
symbol: AgentWebSocketServer._process_request
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_process_request(self, *args: Any) -> Any"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: implicit
  output_contract: implicit
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: medium
  test_coverage: missing
  observability: clear
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
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Missing direct tests for origin allow/reject behavior and both websockets callback shapes."
    evidence: "start() registers this callback with both serve APIs; repository-wide test search found no coverage for the hook or shared ws_origin helpers, including disabled checks, allowed/rejected/missing Origin, and legacy/current response shapes."
    suggested_action: "Add async tests for the feature-disabled path, allowed and rejected hosts, absent Origin with and without the 'none' allowlist token, and both callback shapes."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._process_request`

## Actual Role

WebSocket handshake hook registered with both legacy and current `websockets` servers. It normalizes either callback shape, reads `Origin`, permits every handshake unless `JIUWENSWARM_ENABLE_ORIGIN_CHECK=1`, and otherwise allows configured hostnames (or missing Origin only via the `none` allowlist token) while returning an API-shaped 403 response for rejection.

## Key Signals

- Input: Variable `process_request` callback arguments from legacy or current websockets APIs.
- Output: `None` to continue the handshake, or a forbidden handshake response from `forbidden_origin_response(args)`.
- Main side effects: Emits info/warning logs for origin-check state and rejected origins.
- Main risk: The security check is explicitly fail-open when its environment flag is absent, and neither callback compatibility nor allow/reject policy has regression coverage.
- Related tests: No direct `_process_request` tests or tests of the shared `ws_origin` helpers were found anywhere under `tests/`.

## Detail Index

- Detail docs pending.
