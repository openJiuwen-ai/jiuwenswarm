---
symbol: AgentWebSocketServer._handle_agents_list
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_agents_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:48Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:a15d8777b28294050f8ec636af34f0d5723c9656e9c38c4c1fe5c68d1afc7f6f
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: input_contract
    severity: high
    status: open
    summary: "Workspace identity is incompatible with normal clients and unrestricted when supplied."
    evidence: "Current handler reads only params.workspace_dir and ignores request project_dir/cwd identity.. See AgentWebSocketServer._handle_agents_list/risks.md#issue-001."
    suggested_action: "Resolve a canonical authorized project identity shared with clients; reject arbitrary or ambiguous workspace roots."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "The list endpoint exposes complete agent definitions."
    evidence: "Current response applies dataclass_asdict to every active and shadowed AgentDefinition, including. See AgentWebSocketServer._handle_agents_list/risks.md#issue-002."
    suggested_action: "Return a bounded summary DTO and gate full prompt/path disclosure behind an authorized detail endpoint."
  - id: ISSUE-003
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Synchronous full-content discovery is unbounded on the WebSocket event loop."
    evidence: "Before its first await, AgentConfigService.list_agents scans local/user/project directories. See AgentWebSocketServer._handle_agents_list/risks.md#issue-003."
    suggested_action: "Move file discovery off-loop, cache by file metadata, and paginate/cap list summaries."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Handler and cross-boundary list contracts lack direct coverage."
    evidence: "test_agent_config_service.py covers service-level builtins, project merge, precedence, and sorting, but. See AgentWebSocketServer._handle_agents_list/risks.md#issue-004."
    suggested_action: "Add temporary-directory service tests and TUI/Gateway-to-AgentServer contract tests."
---

# AgentWebSocketServer._handle_agents_list

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_agents_list/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_agents_list/risks.md)
