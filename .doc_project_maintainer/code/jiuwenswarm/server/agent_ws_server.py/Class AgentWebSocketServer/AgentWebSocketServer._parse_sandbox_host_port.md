---
symbol: AgentWebSocketServer._parse_sandbox_host_port
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_parse_sandbox_host_port(url: str) -> tuple[str, int]"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: none
  error_handling: flawed
  state_mutation: none
  dependency_coupling: high
  test_coverage: missing
  observability: missing
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
    severity: high
    status: open
    summary: "Malformed endpoints silently become the local default endpoint."
    evidence: "Missing host uses 127.0.0.1, absent/zero port uses 8321, and exceptions such as invalid ports reset both. Config validates only non-empty text; callers use the substituted endpoint."
    suggested_action: "Require an absolute URL with valid host/port; reject malformed input visibly."
  - id: ISSUE-002
    dimension: input_contract
    severity: high
    status: open
    summary: "Scheme and URL form are ignored even though callers use an HTTP-only runner."
    evidence: "Only hostname/port are used. Bare host:port, unix://, and https:// are accepted/defaulted differently, while JiuwenBoxRunner always constructs an http URL."
    suggested_action: "Validate one central endpoint grammar and persist its canonical form."
  - id: ISSUE-003
    dimension: output_contract
    severity: high
    status: open
    summary: "Parsed runtime endpoint can diverge from the URL retained in configuration and responses."
    evidence: "Enable/bootstrap rewrite URL only when the allocator changes port. Otherwise an unsupported original URL can remain persisted while the runner uses parsed/defaulted TCP values."
    suggested_action: "Return a canonical validated URL together with host/port and make all callers use that single result."
  - id: ISSUE-004
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "IPv6 parsed output is incompatible with downstream networking helpers."
    evidence: "urlparse can return ::1, but bind/allocation use AF_INET and runner health URLs interpolate the unbracketed host as http://::1:port/health."
    suggested_action: "Support IPv6 consistently with address-family resolution and bracketed URL serialization, or reject it explicitly."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No endpoint parser or caller integration tests were found."
    evidence: "No test covers this method, invalid URL forms/ports, IP families, defaults, canonical persistence, or runner targets."
    suggested_action: "Add table-driven parser and caller tests for exact used/persisted endpoints."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._parse_sandbox_host_port`

## Actual Role

Extracts `hostname` and `port` with `urlparse`, defaulting missing parts to `127.0.0.1:8321`. Any error returns that local default; scheme is not validated.

## Key Signals

- Input: Sandbox endpoint string read from shared configuration.
- Output: Host string and integer port, always returning a value even for malformed input.
- Side effects: None; invalid-input fallback is not logged.
- Main risk: Misconfiguration can silently target a different local jiuwenbox service and split persisted versus effective endpoint identity.
- Tests/flow: No direct/caller tests or dedicated sandbox flow; sandbox runtime remains pending in the build plan.

## Detail Index

- Detail docs pending.
