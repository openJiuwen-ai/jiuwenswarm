---
symbol: AgentWebSocketServer._allocate_internal_jiuwenbox_port
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_allocate_internal_jiuwenbox_port(self, host: str, preferred_port: int) -> int"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: clear
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: medium
  test_coverage: missing
  observability: partial
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
    summary: "Allocated ports are not reserved until jiuwenbox binds."
    evidence: "Both bindability probing and bind-to-zero selection close their sockets before JiuwenBoxRunner.ensure_running starts uvicorn. Another process can claim the returned port in that window; startup then times out/fails and callers report failure without allocation retry."
    suggested_action: "Pass a reserved socket/FD into the server, let uvicorn bind port zero and report its port, or retry allocation specifically on EADDRINUSE."
  - id: ISSUE-002
    dimension: error_handling
    severity: medium
    status: open
    summary: "All preferred-port probe failures are labeled as port occupation."
    evidence: "_is_tcp_port_bindable returns false for busy, invalid, unbindable, permission, and IPv6-on-AF_INET cases. The allocator then attempts another AF_INET bind and logs that the preferred port was busy; non-occupation failures may instead raise from _pick_free_tcp_port."
    suggested_action: "Return a typed probe result, validate address family/host/port first, and distinguish occupied from invalid or unsupported endpoints."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Internal jiuwenbox allocation branches are untested."
    evidence: "No test references this allocator, the bind probe, or free-port picker; runner-owned reuse, free preferred, busy fallback, allocation race, invalid host, and IPv6 behavior are uncovered."
    suggested_action: "Add isolated socket/runner tests for every branch and a retry/failure integration case."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._allocate_internal_jiuwenbox_port`

## Actual Role

Chooses the TCP port for an internally managed jiuwenbox instance. It reuses the preferred port when the runner already owns that live endpoint, otherwise returns the preferred port when an IPv4 bind probe succeeds, or asks the kernel for a temporary free port and logs the fallback.

## Key Signals

- Input: Host and preferred port parsed from the configured sandbox endpoint.
- Output: Integer port candidate; it is a point-in-time choice, not a reservation.
- Side effects: Performs temporary socket binds and logs busy-port fallback; it does not mutate runner/config state.
- Call chain: AgentServer bootstrap and `/sandbox enable` call it immediately before `JiuwenBoxRunner.ensure_running`; successful fallback URLs are later persisted.
- Coordination: Runner locking serializes actual lifecycle changes and validates owned host/port/policy matches, but begins only after allocation returns.
- Tests/flow: No allocation tests or dedicated sandbox flow were found.

## Detail Index

- Detail docs pending.
