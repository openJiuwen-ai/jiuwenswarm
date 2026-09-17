---
symbol: main
kind: function
source: jiuwenswarm/server/app_agentserver.py
source_role: runtime_source
audit_scope: default_health_audit
signature: "main() -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: clear
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: low
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  confidence: confirmed
  expired_reason: null
issues: []
confidence: confirmed
details: {}
---

# `main`

## Actual Role

Parses `jiuwenswarm-agentserver` CLI arguments, validates named-instance dotenv early parsing, resolves bind host and port from CLI or environment, and runs `_run` through `asyncio.run`.

## Key Signals

- Input: `--port`, `--name`, `--dotenv`, `AGENT_SERVER_HOST`, `AGENT_SERVER_PORT`, `AGENT_PORT`.
- Output: process exit through `_run`.
- Main side effects: starts asyncio event loop.
- Main risk: port env parsing errors are not directly covered by cited tests.
- Related tests: indirect process-launch system tests; direct `_run` lifecycle test.

## Detail Index

- Detail docs pending.
