---
symbol: AgentWebSocketServer._read_landlock_compatibility
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_read_landlock_compatibility(policy_path: Path | None) -> str"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: clear
  output_contract: weak
  side_effects: none
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
    dimension: output_contract
    severity: medium
    status: open
    summary: "Fallback output hides missing or malformed policy state."
    evidence: "Lines 5095-5109 return best_effort for a missing path/file, unreadable or invalid YAML, non-mapping roots, absent landlock blocks, and blank compatibility. Only parse exceptions get a debug log, so callers cannot distinguish a configured default from degraded inspection."
    suggested_action: "Return structured value/source/error state, or omit compatibility and expose an inspection warning when the policy cannot be read reliably."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Arbitrary non-empty compatibility strings are reported as valid."
    evidence: "The helper strips and returns any string, while JiuwenBox LandlockPolicy accepts only disabled, best_effort, or hard_requirement at jiuwenbox/models/policy.py:431."
    suggested_action: "Validate against the shared enum/model and report invalid policy values explicitly."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "AgentServer Landlock policy inspection is untested."
    evidence: "No test references _read_landlock_compatibility or asserts /sandbox Landlock status for missing, malformed, invalid, disabled, or hard-requirement policies. Jiuwenbox integration tests exercise its own parsed policy, not this reader."
    suggested_action: "Add focused temporary-file tests plus attach-status assertions for every valid value and degraded input class."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._read_landlock_compatibility`

## Actual Role

Reads a resolved jiuwenbox YAML policy file and extracts `landlock.compatibility` for AgentServer `/sandbox` status payloads. It performs no enforcement; absent or unusable input falls back to the display value `best_effort`, while a present non-empty string is returned verbatim after trimming.

## Key Signals

- Input: Optional `Path` to a YAML policy file.
- Output: Compatibility string, currently with `best_effort` as a catch-all fallback.
- Side effects: Reads one local file and may emit a debug log; no state mutation.
- Call chain: `_attach_landlock_status` combines this value with jiuwenbox `/health` capability data for every successful sandbox command response.
- Main risk: Display state can look valid even when policy discovery, parsing, shape, or value validation failed.
- Tests/flow: Jiuwenbox tests cover its validated policy behavior, but no AgentServer reader/status test or dedicated sandbox flow was found.

## Detail Index

- Detail docs pending.
