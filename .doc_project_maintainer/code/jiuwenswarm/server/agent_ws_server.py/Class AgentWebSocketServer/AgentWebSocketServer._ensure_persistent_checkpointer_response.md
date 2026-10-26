---
symbol: AgentWebSocketServer._ensure_persistent_checkpointer_response
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_ensure_persistent_checkpointer_response(request: AgentRequest) -> AgentResponse | None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: clear
  output_contract: clear
  side_effects: explicit
  error_handling: clear
  state_mutation: global
  dependency_coupling: medium
  test_coverage: partial
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
    summary: "Checkpointer readiness has no timeout and is not revalidated after initial setup."
    evidence: "The helper awaits ensure_persistent_checkpointer without a deadline. Its callee may wait on a process-global lock and CheckpointerFactory.create, then permanently short-circuits on _PERSISTENT_CHECKPOINTER_READY without checking the current default backend's health."
    suggested_action: "Bound initialization/lock wait time and define a health or reset path so destructive callers fail closed when a previously initialized backend becomes unavailable."
  - id: ISSUE-002
    dimension: test_coverage
    severity: low
    status: open
    summary: "No direct helper test or live E2A wire assertion for CHECKPOINT_UNAVAILABLE."
    evidence: "Tests assert the legacy-shaped fake encoder output through delete handlers, but search found no test reference to _ensure_persistent_checkpointer_response and targeted pytest could not run in the current environment."
    suggested_action: "Add a direct async unit test for the helper's success/failure return values and a wire-normalization assertion if CHECKPOINT_UNAVAILABLE must remain visible in E2A details."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._ensure_persistent_checkpointer_response`

## Actual Role

Acts as the fail-closed checkpoint-setup guard used before `team.delete` and `session.delete`. It awaits the process-wide DeepAdapter initializer and returns `None` on success; any ordinary exception is logged and normalized into a metadata-preserving `CHECKPOINT_UNAVAILABLE` `AgentResponse`.

## Key Signals

- Input: Active `AgentRequest`; uses request id, channel id, and metadata in the fallback response.
- Output: `None` when persistent checkpointer setup succeeds, or a structured error `AgentResponse` when setup fails.
- Main side effects: May initialize the process-wide sqlite persistence checkpointer and logs exceptions on failure.
- Main risk: Initialization and lock waits are unbounded, while a process-wide ready flag suppresses later backend health checks; behavior is only tested through delete handlers.
- Related tests: Adjacent tests cover `team.delete` and `session.delete` checkpointer-unavailable responses and session.delete success initialization.

## Detail Index

- Detail docs pending.
