---
symbol: AgentWebSocketServer._is_tcp_port_bindable
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_is_tcp_port_bindable(host: str, port: int) -> bool"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: partial
  state_mutation: none
  dependency_coupling: low
  test_coverage: missing
  observability: not_applicable
  performance_risk: low
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  audited_symbol_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Port probing supports IPv4 only while endpoint parsing accepts IPv6 hosts."
    evidence: "Line 5192 always creates AF_INET. _parse_sandbox_host_port can return an IPv6 hostname such as ::1; bind then returns false, and the allocator's AF_INET-only _pick_free_tcp_port also fails instead of selecting an IPv6 port or rejecting configuration early."
    suggested_action: "Resolve the address family with getaddrinfo and use it consistently for probing/allocation, or validate and clearly reject non-IPv4 sandbox endpoints."
  - id: ISSUE-002
    dimension: error_handling
    severity: low
    status: open
    summary: "Only bind-time socket errors are normalized to false."
    evidence: "socket.socket is created before the try/finally, so descriptor exhaustion or address-family creation failure propagates, while bind OSError is collapsed to false without diagnostics."
    suggested_action: "Define whether infrastructure errors should propagate distinctly and add diagnostic context instead of treating every bind failure as simple occupation."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Port availability and allocator integration are untested."
    evidence: "No test references _is_tcp_port_bindable, _pick_free_tcp_port, or _allocate_internal_jiuwenbox_port; free, occupied, invalid, IPv6, and runner-owned cases are uncovered."
    suggested_action: "Add loopback socket tests for free/busy ports and table-driven family/error cases, plus allocator branch tests."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._is_tcp_port_bindable`

## Actual Role

Performs a short-lived IPv4 TCP bind probe for a specific host and port. It returns `True` only when the bind succeeds, converts bind-time `OSError` into `False`, and always closes a successfully created socket; it does not listen, reserve the port, or identify the current owner.

## Key Signals

- Input: Host string and integer port supplied by sandbox endpoint parsing.
- Output: Boolean point-in-time bindability result for an IPv4 socket.
- Side effects: Temporarily acquires and releases a local socket binding; no persistent state.
- Call chain: `_allocate_internal_jiuwenbox_port` uses it after checking runner ownership; false selects `_pick_free_tcp_port`. Bootstrap and `/sandbox enable` both consume that allocator.
- Main risk: Address-family mismatch and the inherent probe-to-spawn race; the latter is detected later when uvicorn startup fails.
- Tests/flow: No port-probe/allocation tests or dedicated sandbox flow were found.

## Detail Index

- Detail docs pending.
