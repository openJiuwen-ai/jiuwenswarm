---
symbol: AgentWebSocketServer._resolve_code_language
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_resolve_code_language() -> str"
health:
  overall: watch
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: partial
  input_contract: clear
  output_contract: weak
  side_effects: none
  error_handling: partial
  state_mutation: none
  dependency_coupling: medium
  test_coverage: missing
  observability: missing
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
    summary: "No production call site remains for the helper."
    evidence: "Repository search found only this definition and two test monkeypatch assignments; current code-mode preparation and plan-approval paths never call the helper."
    suggested_action: "Remove the helper and stale monkeypatches if plan-approval language is no longer server-owned, or wire it into a real call path if still required."
  - id: ISSUE-002
    dimension: input_contract
    severity: medium
    status: open
    summary: "The helper reads a noncanonical top-level language key."
    evidence: "It returns config.get(\"language\", \"cn\") without type/value normalization, while shipped config and mutators use preferred_language with zh/en normalization."
    suggested_action: "If retained, resolve from preferred_language with the same normalization and mapping used by current code-mode language policy."
  - id: ISSUE-003
    dimension: test_coverage
    severity: low
    status: open
    summary: "Missing direct tests for configured language, fallback, and failure behavior."
    evidence: "Existing tests only assign server._resolve_code_language = MagicMock(return_value=\"cn\"); no test calls the real method."
    suggested_action: "Add direct unit tests for preferred/default language behavior and get_config() failure, or delete the helper with the stale test setup."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._resolve_code_language`

## Actual Role

Orphaned static helper that reads global config and returns the top-level `language` value, defaulting to `"cn"` when the key is missing or any exception occurs. It does not enforce the documented `"cn" | "en"` result, and current code-mode language policy instead consumes normalized `preferred_language` values.

## Key Signals

- Input: None; depends on global config file access through `get_config()`.
- Output: Intended `"cn"` or `"en"`; actually any value stored under `language`, including non-string values.
- Main side effects: None directly, aside from config reads inside `get_config()`.
- Main risk: If reintroduced into a call path, its stale key and unvalidated return can select the wrong language while config-read failures remain silent.
- Related tests: No direct test invokes the method; two plan-mode tests only monkeypatch the attribute.

## Detail Index

- Detail docs pending.
