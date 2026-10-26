---
symbol: AgentWebSocketServer._handle_agents_delete
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_agents_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: persistent
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
  audited_symbol_hash: sha256:19c2fb249c49e078572972607825e11fe42d5dae681d56c0066b11a5be80162f
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "The request selects an arbitrary workspace and ambiguous definition source."
    evidence: "At HEAD 39feee89, params.workspace_dir is passed directly to AgentConfigService and name is neither. See AgentWebSocketServer._handle_agents_delete/risks.md#issue-001."
    suggested_action: "Resolve an authorized project identity and require expected source/revision before unlinking."
  - id: ISSUE-002
    dimension: state_mutation
    severity: high
    status: open
    summary: "File deletion, global config mutation, and runtime reload are not transactional."
    evidence: "At HEAD 39feee89, service.delete_agent unlinks first, remove_subagent_from_config edits global config. See AgentWebSocketServer._handle_agents_delete/risks.md#issue-002."
    suggested_action: "Use a recoverable transaction: stage the file, atomically update config, reload, then commit or restore/retry."
  - id: ISSUE-003
    dimension: output_contract
    severity: high
    status: open
    summary: "A missing definition still mutates config and returns top-level success."
    evidence: "At HEAD 39feee89, service.delete_agent can return false for an unknown name, but the handler still. See AgentWebSocketServer._handle_agents_delete/risks.md#issue-003."
    suggested_action: "Validate first and distinguish not-found, deleted, orphan cleanup, partial failure, and success at top level."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Helper tests exist, but the delete RPC lifecycle is untested."
    evidence: "Service/helper tests cover basic deletion and config round trips. No direct RPC/handler test covers. See AgentWebSocketServer._handle_agents_delete/risks.md#issue-004."
    suggested_action: "Add handler/Gateway contracts, failure/concurrency cases, and an agent-configuration flow."
  - id: ISSUE-005
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Synchronous deletion and config reads/writes run on the WebSocket event loop."
    evidence: "At HEAD 39feee89, AgentConfigService construction/deletion, remove_subagent_from_config, and get_config. See AgentWebSocketServer._handle_agents_delete/risks.md#issue-005."
    suggested_action: "Move persistent agent/config mutations behind an async serialized service or worker thread, preserving atomic ordering."
---

# AgentWebSocketServer._handle_agents_delete

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_agents_delete/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_agents_delete/risks.md)
