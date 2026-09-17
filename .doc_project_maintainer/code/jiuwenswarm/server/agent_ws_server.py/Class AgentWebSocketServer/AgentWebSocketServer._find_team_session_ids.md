---
symbol: AgentWebSocketServer._find_team_session_ids
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_find_team_session_ids(team_name: str) -> list[str]"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: partial
  state_mutation: none
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: medium
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
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Destructive selection lacks an explicit metadata authority contract."
    evidence: "It uses process-local cached metadata, while Gateway can write metadata cross-process and AgentServer team identity writes may remain newer in cache before async disk flush. Either cache or disk can be stale when this feeds team.delete."
    suggested_action: "Define ownership/versioning for mode/team_name and reconcile cache plus disk before destructive selection."
  - id: ISSUE-002
    dimension: error_handling
    severity: low
    status: open
    summary: "Filesystem scan errors are not locally normalized."
    evidence: "The helper handles a missing sessions root but does not catch iterdir/is_dir failures; dispatcher-level exception handling would turn this into a generic request failure."
    suggested_action: "Wrap the scan with targeted logging and return or raise a deliberate team-delete error shape."
  - id: ISSUE-003
    dimension: test_coverage
    severity: low
    status: open
    summary: "Boundary and freshness cases are not directly covered."
    evidence: "Direct test covers mode/team_name filtering only; team-delete tests override the helper."
    suggested_action: "Test missing/non-directory roots, cache/disk divergence, malformed metadata, and multi-match sorting."
  - id: ISSUE-004
    dimension: performance_risk
    severity: low
    status: open
    summary: "The async helper performs an unbounded synchronous scan."
    evidence: "It iterates every session directory and reads metadata synchronously on the event loop before team deletion can continue."
    suggested_action: "Use indexed team metadata or offload the scan when session counts can be large."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._find_team_session_ids`

## Actual Role

Enumerates the AgentServer sessions directory and returns a deterministic list of session IDs whose metadata says `mode: team` and whose trimmed `team_name` exactly matches the caller-provided team name. It is a read-only selector used by `team.delete` before runtime shutdown, OpenJiuwen team deletion, session directory removal, and metadata-cache clearing.

## Key Signals

- Input: Pre-trimmed team name string from `_handle_team_delete`.
- Output: Sorted unique `list[str]` of matching local session directory names; empty list when the sessions root is absent.
- Main side effects: No writes; performs synchronous filesystem iteration and metadata reads.
- Main risk: Cache/disk freshness ambiguity and scan failures can change destructive team-delete selection.
- Related tests: One direct filter test exists; team-delete tests stub this helper. Freshness, root errors, malformed metadata, multi-match ordering, and the partial session lifecycle flow remain gaps.

## Detail Index

- Detail docs pending.
