---
symbol: AgentWebSocketServer._handle_hooks_list
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_hooks_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
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
  audited_at: 2026-07-14T11:39:38Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:6c4eefe3a47332faf52d0c2cf70a45eb3a941b0236d00a982fd1549633f20581
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: medium
    status: open
    summary: "The summary response exposes complete, unbounded hook definitions."
    evidence: "At HEAD 39feee89, get_event_summary is returned verbatim as events with no redaction or size bound. The. See AgentWebSocketServer._handle_hooks_list/risks.md#issue-001."
    suggested_action: "Define a privileged detail contract or return bounded, redacted metadata."
  - id: ISSUE-002
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "The AgentServer RPC is bypassed by duplicated Gateway implementations."
    evidence: "At HEAD 39feee89, Web and TUI register independent local hooks.list handlers while _handle_message also. See AgentWebSocketServer._handle_hooks_list/risks.md#issue-002."
    suggested_action: "Choose one authoritative layer, deprecate the orphan copy, and test retained surfaces against one contract."
  - id: ISSUE-003
    dimension: input_contract
    severity: medium
    status: open
    summary: "Malformed matcher hook collections are accepted and summarized misleadingly."
    evidence: "At HEAD 39feee89, load_hooks_config accepts entry.get('hooks', []) without enforcing a list of valid. See AgentWebSocketServer._handle_hooks_list/risks.md#issue-003."
    suggested_action: "Validate matcher and hook objects during load and type the response schema."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Only the summary helper is tested; the RPC and three-surface parity are not."
    evidence: "At HEAD 39feee89, test_hooks_config.py covers get_event_summary helper behavior, but no direct test. See AgentWebSocketServer._handle_hooks_list/risks.md#issue-004."
    suggested_action: "Add wire, malformed-config, payload-boundary, routing, and surface-parity tests."
  - id: ISSUE-005
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Global config parsing and unbounded payload construction run synchronously on the event loop."
    evidence: "At HEAD 39feee89, the async handler calls synchronous get_config, load_hooks_config, and. See AgentWebSocketServer._handle_hooks_list/risks.md#issue-005."
    suggested_action: "Cache a validated bounded summary or move config loading/serialization off-loop with response quotas."
---

# AgentWebSocketServer._handle_hooks_list

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_hooks_list/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_hooks_list/risks.md)
