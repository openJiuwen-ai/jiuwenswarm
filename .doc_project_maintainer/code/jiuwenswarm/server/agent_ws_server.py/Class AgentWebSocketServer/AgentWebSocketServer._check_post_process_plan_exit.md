---
symbol: AgentWebSocketServer._check_post_process_plan_exit
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_check_post_process_plan_exit(request: AgentRequest, agent: Any) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: missing
  state_mutation: shared
  dependency_coupling: high
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
    dimension: error_handling
    severity: medium
    status: open
    summary: "Post-process plan-exit check can mask or replace the primary request failure."
    evidence: "The method awaits create_agent_session, pre_run, load_state, and _push_plan_mode_exited without local error handling, and both unary and stream handlers await it inside finally blocks after message processing."
    suggested_action: "Wrap the post-process check or its call sites so failures are logged without replacing the original process_message or stream failure."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct tests cover successful detection, skip branches, or failure behavior."
    evidence: "Repository search found no test invoking _check_post_process_plan_exit; closest coverage only exercises _ensure_code_mode_state with a preseeded _plan_exited_sessions flag."
    suggested_action: "Add focused async tests for no session, non-code/non-plan skip, plan still active, normal-state push, and load_state/send_push exception handling."
  - id: ISSUE-003
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Post-process detection is not serialized with per-session mode transitions."
    evidence: "Unlike _ensure_code_mode_state, this method does not acquire _session_mode_sync_lock. A following request can enter plan mode between its checkpoint read and exit push, leaving the exit flag set and emitting a stale event for the new turn."
    suggested_action: "Serialize the state check, flag mutation, and push decision with the same per-session lock; make concurrent delivery idempotent."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._check_post_process_plan_exit`

## Actual Role

After a `code.plan` request finishes, reloads persisted state to detect whether approved `exit_plan_mode` restored mode to `normal`. It records the session in `_plan_exited_sessions`, sends `plan.mode_exited`, and logs. Unary and streaming handlers await it from processing `finally` blocks.

## Key Signals

- Input: `AgentRequest` with a non-empty `session_id` and mode resolving to `code.plan`; `agent` must expose `get_instance().card` and `load_state(session)`.
- Output: None.
- Main side effects: Canonicalizes `request.params["mode"]`, reads persisted agent state, mutates `_plan_exited_sessions`, sends a server push, and logs.
- Main risks: Failures from this secondary hook can replace the primary processing failure; its unlocked read/flag/push sequence can race with the next request's locked mode transition and emit a stale exit notification.
- Related tests: `test_plan_mode_orchestration.py` covers `_ensure_code_mode_state` and explicit re-entry with a preseeded exit flag, but not this method. The `agentserver-plan-mode-exit` flow slice remains pending in `project/build-plan.md`.

## Detail Index

- Detail docs pending.
