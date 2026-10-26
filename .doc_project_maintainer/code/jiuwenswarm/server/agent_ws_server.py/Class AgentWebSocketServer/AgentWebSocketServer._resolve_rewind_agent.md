---
symbol: AgentWebSocketServer._resolve_rewind_agent
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_resolve_rewind_agent(channel_id: str) -> tuple[Any, Any] | None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: partial
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
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Channel-only lookup can select the wrong cached agent."
    evidence: "AgentManager caches multiple mode/sub-mode/project identities per channel, but this method passes only channel_id; get_agent_nowait then returns the first channel entry before any preferred-agent fallback."
    suggested_action: "Resolve with request/session mode, sub-mode, and project identity, or maintain an explicit session-to-agent mapping."
  - id: ISSUE-002
    dimension: error_handling
    severity: medium
    status: open
    summary: "Malformed or stale wrappers raise instead of resolving unavailable state."
    evidence: "agent.get_instance() and deep_agent.react_agent are unguarded. Full rewind may hit this after history truncation; rewind_context invokes the resolver outside its local try block."
    suggested_action: "Guard wrapper/instance access and normalize lookup failure before destructive rewind work."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct tests pin resolver selection and failure behavior."
    evidence: "Only lower-level rewind_session_context tests were found; resolver branches, default normalization, and multi-agent channel selection are untested."
    suggested_action: "Add focused fake-AgentManager tests, including multiple mode/project entries in one channel."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._resolve_rewind_agent`

## Actual Role

Selects an already-created wrapper by channel, unwraps its DeepAgent and `react_agent`, and returns both for context reconstruction. It creates no runtime; callers treat missing state differently—full rewind reports partial context failure, while context-only rewind returns an error.

## Key Signals

- Input: `channel_id`; blank values normalize to `"default"`; session/mode/project identity is unavailable.
- Output: `(deep_agent, react_agent)` tuple, or `None`.
- Main side effects: None; callers mutate and persist the selected agent's session context.
- Main risk: ambiguous selection when one channel contains multiple cached agent identities.
- Related tests: lower-level context reconstruction and AgentManager project-cache identity have coverage; this resolver does not.

## Detail Index

- Detail docs pending.
