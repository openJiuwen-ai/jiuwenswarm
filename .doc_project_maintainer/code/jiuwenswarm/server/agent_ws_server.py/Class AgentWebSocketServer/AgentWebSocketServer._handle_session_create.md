---
symbol: AgentWebSocketServer._handle_session_create
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_create(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: critical
  name_behavior_match: mismatch
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: hidden
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: audit_expired
  auditor: codex
  audited_at: 2026-07-14T11:39:39Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:33487351f0a252dd739869feacd05acef75371d18829a892b7f4d27be460572e
  confidence: confirmed
  expired_reason: "Implementation changed through 2026-08-18 for AgentServer-owned allocation, scoped TUI explicit-ID compatibility, and immutable persist_session creation metadata; no independent symbol health re-audit was performed."
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: critical
    status: fixed
    summary: "Untrusted explicit session IDs become authoritative filesystem identities."
    evidence: "Normal create rejects explicit IDs outside TUI; the TUI compatibility branch sanitizes the ID, enforces a 128-character limit, and is channel-restricted. See AgentWebSocketServer._handle_session_create/risks.md#issue-001."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: high
    status: fixed
    summary: "Create neither persists nor uniquely reserves a session."
    evidence: "Creation claims an AgentServer ID and writes metadata; TUI explicit-ID creation writes or reuses metadata while holding a per-ID lock. See AgentWebSocketServer._handle_session_create/risks.md#issue-002."
  - id: ISSUE-003
    dimension: side_effects
    severity: high
    status: fixed
    summary: "Team creation can stop distributed runtimes before success is observable."
    evidence: "For resolved mode 'team', current code awaits TeamManager.prepare_session_switch before encoding or. See AgentWebSocketServer._handle_session_create/risks.md#issue-003."
    suggested_action: "Separate creation from switching; make switching recoverable and classify send failures before retrying."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Tests cover mocked success and one successful team switch only."
    evidence: "Direct tests now cover normal creation, Persist Session locking and input validation, TUI immutable-state conflict, explicit-ID idempotency/concurrency, unsafe and cross-channel IDs, stable binding, warm bypass, and team preparation. See AgentWebSocketServer._handle_session_create/risks.md#issue-004."
---

# AgentWebSocketServer._handle_session_create

## Actual Role

Handles both AgentServer-allocated `session.create` and its scoped TUI explicit-ID compatibility form. It validates project/work-mode identity and the boolean `persist_session` creation field, includes that field in create-token idempotency without widening the warm key, claims an eligible warm or warming Session for normal creation, or validates and serializes a caller-supplied TUI ID while bypassing prewarm. It persists or restores authoritative immutable Persist Session metadata, runs the switch-owner lifecycle, and returns normalized identity/status. The health audit remains expired pending an independent re-audit.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_create/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_create/risks.md)
