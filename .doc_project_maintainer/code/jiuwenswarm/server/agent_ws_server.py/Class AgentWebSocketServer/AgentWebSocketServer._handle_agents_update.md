---
symbol: AgentWebSocketServer._handle_agents_update
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_agents_update(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
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
  state_mutation: persistent
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
  audited_symbol_hash: sha256:aeb04265b8a6608f65dd8e911fea42d410d756d3a584fd9b6e5cefc9b64516c9
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "The request can select an arbitrary host workspace for a persistent write."
    evidence: "Lines 5658-5675 accept workspace_dir from request params and pass it directly to AgentConfigService. The. See AgentWebSocketServer._handle_agents_update/risks.md#issue-001."
    suggested_action: "Resolve and canonicalize authenticated project identity; reject roots/symlink targets outside it."
  - id: ISSUE-002
    dimension: state_mutation
    severity: high
    status: open
    summary: "Concurrent updates can lose data and writes are not crash-atomic."
    evidence: "AgentConfigService.update_agent resolves the active definition, mutates the in-memory object, then. See AgentWebSocketServer._handle_agents_update/risks.md#issue-002."
    suggested_action: "Use per-definition locking plus revision/ETag conflict detection, and write through a temporary file followed by atomic."
  - id: ISSUE-003
    dimension: output_contract
    severity: medium
    status: open
    summary: "A persisted update can be reported as RPC success while live agents remain stale."
    evidence: "Line 5675 overwrites the definition before lines 5680-5685 attempt global hot reload. Reload failure is. See AgentWebSocketServer._handle_agents_update/risks.md#issue-003."
    suggested_action: "Return a failed/degraded RPC that clients must surface, or roll back/retry reload and provide explicit recovery."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Service happy paths exist, but the update RPC boundary is untested."
    evidence: "Service tests cover field update plus builtin/nonexistent rejection. Static search found no direct. See AgentWebSocketServer._handle_agents_update/risks.md#issue-004."
    suggested_action: "Add handler/Gateway contracts, concurrent/failed-write and reload-failure cases, and a flow doc."
  - id: ISSUE-005
    dimension: input_contract
    severity: medium
    status: open
    summary: "Unknown or empty update fields can be reported as a successful update."
    evidence: "Lines 5672-5673 silently retain only UpdateAgentParams dataclass fields. Typos and unsupported keys are. See AgentWebSocketServer._handle_agents_update/risks.md#issue-005."
    suggested_action: "Reject unknown fields and require at least one recognized effective change; compare normalized before/after definitions."
---

# AgentWebSocketServer._handle_agents_update

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_agents_update/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_agents_update/risks.md)
