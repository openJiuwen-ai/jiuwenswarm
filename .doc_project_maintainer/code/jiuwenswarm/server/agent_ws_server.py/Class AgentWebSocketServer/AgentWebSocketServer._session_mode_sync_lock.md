---
symbol: AgentWebSocketServer._session_mode_sync_lock
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_session_mode_sync_lock(session_id: str) -> asyncio.Lock"
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
  side_effects: implicit
  error_handling: clear
  state_mutation: global
  dependency_coupling: medium
  test_coverage: partial
  observability: not_applicable
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
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Per-session mode-sync locks are never removed."
    evidence: "The global dict is defined near module startup and this method stores missing locks, but searches found no pop or clear path; reset_instance, stop, and session delete do not clear _session_mode_sync_locks."
    suggested_action: "Add a guarded cleanup path for completed or deleted sessions, and clear the registry during test-only singleton reset or full server shutdown if no sync is in flight."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct test verifies the per-session serialization contract or registry lifetime."
    evidence: "Plan-mode orchestration tests cover _ensure_code_mode_state transitions and skip paths, but no direct reference to _session_mode_sync_lock or _session_mode_sync_locks and no same-session contention test was found."
    suggested_action: "Add focused async tests proving same-session calls share and serialize on one lock, different sessions do not block each other, and cleanup cannot remove a locked in-flight guard."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Module-global locks can outlive the server event loop that used them."
    evidence: "The registry survives reset_instance and server stop; asyncio.Lock is loop-bound when its contended acquire path creates waiters, so a cached lock can be reused by a later server/test loop."
    suggested_action: "Scope the registry to the server/loop or clear only quiescent locks during shutdown and test reset."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._session_mode_sync_lock`

## Actual Role

Returns the module-global cached `asyncio.Lock` for a session key, creating it synchronously on first access. `_ensure_code_mode_state` holds that lock across checkpoint load, mode comparison/switch, persistence, and exit notification so same-session transitions serialize while different keys proceed independently; the cache spans server instances and event loops.

## Key Signals

- Input: `session_id` string; caller uses `request.session_id` or `"default"`.
- Output: Reused `asyncio.Lock` for that session key.
- Main side effects: Mutates module-global `_session_mode_sync_locks` on cache miss.
- Main risk: Locks live indefinitely across deleted sessions, server instances, and event loops, creating unbounded retention and possible loop-affinity failures after contended use.
- Related tests: Indirect `_ensure_code_mode_state` behavior tests exist; no direct lock, concurrency, or lifetime test was found.

## Detail Index

- Detail docs pending.
