---
symbol: AgentWebSocketServer._handle_agents_set_enabled
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_agents_set_enabled(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, enabled: bool) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: implicit
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
  audited_at: 2026-07-14T11:39:33Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:50c757c4cea53b45c1223a326d3165984c1973101270b86443bb3f73ae4b07e6
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "Config mutation is not rolled back when runtime reload fails."
    evidence: "Current sequence calls upsert_subagent_in_config before reload_agents_config. A reload exception is. See AgentWebSocketServer._handle_agents_set_enabled/risks.md#issue-001."
    suggested_action: "Apply atomically or restore the previous config value when reload fails."
  - id: ISSUE-002
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "Workspace validation leads to a global enable state and global reload."
    evidence: "workspace_dir scopes AgentConfigService.get_agent validation, but upsert_subagent_in_config writes. See AgentWebSocketServer._handle_agents_set_enabled/risks.md#issue-002."
    suggested_action: "Define global versus workspace ownership and reload only affected runtimes."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Concurrent agent/config commands can lose YAML updates."
    evidence: "AgentServer request tasks run concurrently, while config upsert performs a shared YAML read-modify-write. See AgentWebSocketServer._handle_agents_set_enabled/risks.md#issue-003."
    suggested_action: "Serialize shared config mutations with one lock and use atomic file replacement."
  - id: ISSUE-004
    dimension: output_contract
    severity: high
    status: open
    summary: "The primary TUI reports success even when applied is false."
    evidence: "On reload failure the method returns top-level ok=true plus applied=false/reload_error. The primary UI. See AgentWebSocketServer._handle_agents_set_enabled/risks.md#issue-004."
    suggested_action: "Return transport failure or require clients to surface partial application explicitly."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "Enable/disable orchestration lacks direct handler coverage."
    evidence: "Service/config helper tests cover agent lookup and YAML upsert behavior, but none invokes. See AgentWebSocketServer._handle_agents_set_enabled/risks.md#issue-005."
    suggested_action: "Add handler tests with temp config, fake manager, concurrent changes, and TUI assertions."
---

# AgentWebSocketServer._handle_agents_set_enabled

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_agents_set_enabled/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_agents_set_enabled/risks.md)
