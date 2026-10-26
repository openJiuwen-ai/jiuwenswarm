---
symbol: AgentWebSocketServer._pick_free_tcp_port
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_pick_free_tcp_port(host: str) -> int"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: weak
  output_contract: clear
  side_effects: explicit
  error_handling: implicit
  state_mutation: none
  dependency_coupling: low
  test_coverage: missing
  observability: none
  performance_risk: low
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: medium
    status: open
    summary: "The returned port is observed free but never reserved."
    evidence: "The socket binds host:0, reads the assigned port, then closes on return. _allocate_internal_jiuwenbox_port passes that integer to ensure_running later, so another process or concurrent enable can bind it in the gap."
    suggested_action: "Pass an inherited bound socket/file descriptor to the child, or add bounded EADDRINUSE retry around allocation and startup."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "The generic host parameter is implemented as IPv4-only."
    evidence: "The socket is always AF_INET. _parse_sandbox_host_port can yield an IPv6 hostname such as ::1, for which bind raises instead of selecting the matching address family; hostname resolution policy is also implicit."
    suggested_action: "Resolve the host with getaddrinfo and bind the selected family, or explicitly validate/document IPv4-only internal endpoints."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "Collision and bind failures have no local recovery."
    evidence: "socket creation/bind errors propagate directly. A post-selection collision makes ensure_running fail; command enable reports failure and bootstrap only logs, despite the failure being retryable with another ephemeral port."
    suggested_action: "Classify address errors and retry allocation/start atomically for a bounded number of attempts with clear final diagnostics."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No free-port allocator or fallback-start tests were found."
    evidence: "No tests cover valid allocation, busy preferred ports, collision retry, invalid hosts, IPv6, socket cleanup, concurrent callers, command failure, or bootstrap degradation."
    suggested_action: "Add socket-isolated unit tests and runner integration tests with injected bind/start failures."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._pick_free_tcp_port`

## Actual Role

Opens an IPv4 TCP socket, binds the requested host with port zero, returns the kernel-assigned port, and immediately releases it. The internal jiuwenbox allocator uses this only when its preferred port is busy, before a later runner call starts uvicorn on the returned port.

## Key Signals

- Input: Host string expected to be accepted by an IPv4 socket bind.
- Output: Ephemeral port number; socket errors propagate.
- Side effects: Briefly binds and releases a local TCP endpoint.
- Main risk: The result is a racy availability observation, not a reservation.
- Tests/flow: No direct or adjacent tests found; no port-allocation flow exists.

## Detail Index

- Detail docs pending.
