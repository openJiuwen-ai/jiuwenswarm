---
symbol: AgentWebSocketServer._handle_agents_get
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_agents_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
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
  side_effects: implicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:31Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:6c8af01b355f80a09f62d9c905e2c2dcd735037442eb73c5a4ab3969d231104f
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "An unvalidated workspace selector can disclose complete agent definitions."
    evidence: "Current handler passes params.workspace_dir directly to AgentConfigService and serializes the resulting. See AgentWebSocketServer._handle_agents_get/risks.md#issue-001."
    suggested_action: "Resolve an authenticated project identity, constrain it to authorized roots, and omit host paths unless required."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: medium
    status: open
    summary: "Builtin enabled state can remain stale across reads."
    evidence: "AgentConfigService.get_agent calls list_agents, whose sources use list(BUILTIN_AGENTS), a shallow list. See AgentWebSocketServer._handle_agents_get/risks.md#issue-002."
    suggested_action: "Clone builtin definitions for every resolution or explicitly reset derived fields before applying the current config."
  - id: ISSUE-003
    dimension: performance_risk
    severity: medium
    status: open
    summary: "A single-name lookup synchronously parses every agent file on the event loop."
    evidence: "Before the first await, service.get_agent invokes list_agents, scans local/user/project directories. See AgentWebSocketServer._handle_agents_get/risks.md#issue-003."
    suggested_action: "Move discovery off-loop and use a cached, precedence-aware lookup for the normalized name."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Handler and boundary contracts lack direct tests."
    evidence: "test_agent_config_service.py covers service get/list behavior, but repository search finds no. See AgentWebSocketServer._handle_agents_get/risks.md#issue-004."
    suggested_action: "Cover valid/not-found/missing names, source precedence, workspace authorization, stale enabled state, malformed/large."
  - id: ISSUE-005
    dimension: input_contract
    severity: medium
    status: open
    summary: "Missing and malformed agent names are treated as ordinary not-found lookups."
    evidence: "The handler defaults name to an empty string and does no type/format normalization; get_agent compares. See AgentWebSocketServer._handle_agents_get/risks.md#issue-005."
    suggested_action: "Require a normalized string name and distinguish validation failure from a stable not-found result."
---

# AgentWebSocketServer._handle_agents_get

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_agents_get/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_agents_get/risks.md)
