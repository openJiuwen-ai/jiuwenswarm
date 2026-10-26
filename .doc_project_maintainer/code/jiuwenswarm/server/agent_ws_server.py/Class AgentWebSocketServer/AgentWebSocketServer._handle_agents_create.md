---
symbol: AgentWebSocketServer._handle_agents_create
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_agents_create(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:32Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:afa4092de62ef5a1b8ff5b12696f66da79000281d7c1d7917ca3b3c920ce08af
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "Creation, enablement, and runtime reload are not transactional."
    evidence: "See AgentWebSocketServer._handle_agents_create/risks.md#issue-001 for full evidence."
    suggested_action: "Commit atomically or roll back and return structured partial failure."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Create silently overwrites an existing custom agent."
    evidence: "See AgentWebSocketServer._handle_agents_create/risks.md#issue-002 for full evidence."
    suggested_action: "Reject existing custom names by default and require an explicit replace/update."
  - id: ISSUE-003
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "A workspace-scoped request triggers global config and global runtime reload."
    evidence: "See AgentWebSocketServer._handle_agents_create/risks.md#issue-003 for full evidence."
    suggested_action: "Define ownership and reload only affected runtimes."
  - id: ISSUE-004
    dimension: input_contract
    severity: medium
    status: open
    summary: "Unknown request fields are silently discarded and generate is not type-validated."
    evidence: "See AgentWebSocketServer._handle_agents_create/risks.md#issue-004 for full evidence."
    suggested_action: "Reject extras and type-check controls before side effects."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "Handler orchestration and cross-boundary effects lack direct tests."
    evidence: "See AgentWebSocketServer._handle_agents_create/risks.md#issue-005 for full evidence."
    suggested_action: "Add async handler tests with fake generation, temp state, failures, and."
  - id: ISSUE-006
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Unvalidated workspace_dir controls the agent-definition write root."
    evidence: "See AgentWebSocketServer._handle_agents_create/risks.md#issue-006 for full evidence."
    suggested_action: "Resolve workspace from authenticated request project identity and enforce."
---

# AgentWebSocketServer._handle_agents_create

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_agents_create/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_agents_create/risks.md)
