---
symbol: AgentWebSocketServer._handle_config_cache_clear
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_config_cache_clear(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: partial
  input_contract: clear
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:34Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:00eeb58699875b23034af5f2484e3a4e75378dd389b38ca97533e8735bb9c1ab
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "The generic cache-clear RPC invalidates only a narrow memory-config dictionary."
    evidence: "Current handler only calls memory.config.clear_config_cache, which assigns that module's. See AgentWebSocketServer._handle_config_cache_clear/risks.md#issue-001."
    suggested_action: "Remove the endpoint or route it through scoped agent reload and report actual refreshed state."
  - id: ISSUE-002
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "The request enum and handler are orphaned from current Gateway callers."
    evidence: "Current production Web/TUI helpers named _clear_agent_config_cache send AGENT_RELOAD_CONFIG. Repository. See AgentWebSocketServer._handle_config_cache_clear/risks.md#issue-002."
    suggested_action: "Deprecate and remove the stale method after compatibility review, or document a real caller and align naming/semantics."
  - id: ISSUE-003
    dimension: output_contract
    severity: medium
    status: open
    summary: "cleared=true overstates configuration application."
    evidence: "The response always reports cleared=true after assigning None, although active components retain their. See AgentWebSocketServer._handle_config_cache_clear/risks.md#issue-003."
    suggested_action: "Return explicit invalidated/reloaded scopes and configuration revision, with degraded/error state when active."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No cache-clear RPC or lifecycle test exists."
    evidence: "No test references config.cache_clear, CONFIG_CACHE_CLEAR, _handle_config_cache_clear, or. See AgentWebSocketServer._handle_config_cache_clear/risks.md#issue-004."
    suggested_action: "Add wire-level compatibility tests or delete the endpoint; cover cache invalidation, active memory-manager refresh."
  - id: ISSUE-005
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Cache invalidation can race with an in-progress reload."
    evidence: "clear_config_cache and memory.config._load_config share _config_cache without a lock or generation. If. See AgentWebSocketServer._handle_config_cache_clear/risks.md#issue-005."
    suggested_action: "Serialize cache reads/clears or use a revision token so pre-clear loads cannot publish afterward."
---

# AgentWebSocketServer._handle_config_cache_clear

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_config_cache_clear/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_config_cache_clear/risks.md)
