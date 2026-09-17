---
symbol: AgentWebSocketServer.get_conversation_history
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "get_conversation_history(session_id: str, page_idx: int) -> dict[str, Any] | None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: unsafe
  input_contract: weak
  output_contract: partial
  side_effects: implicit
  error_handling: flawed
  state_mutation: external
  dependency_coupling: medium
  test_coverage: weak
  observability: weak
  performance_risk: high
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
    summary: "Caller-controlled session IDs can escape the sessions directory."
    evidence: "Only whitespace is rejected before history_exists/load_history_records. _session_dir joins the raw ID under get_agent_sessions_dir() without blocking absolute paths, '..', or checking containment; read helpers also call it with create=True. Both WebSocket callers pass request params directly."
    suggested_action: "Enforce canonical IDs and resolved containment; make reads use create=False."
  - id: ISSUE-002
    dimension: performance_risk
    severity: high
    status: open
    summary: "Each 20-record page reads and copies the complete unbounded history."
    evidence: "The loader reads the whole JSON/JSONL file; this method then builds restorable and reversed copies before slicing. Deep pages repeat O(total records and bytes) work for both callers."
    suggested_action: "Use bounded storage-level reverse cursor pagination."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "Unreadable/corrupt history becomes a successful empty or partial page."
    evidence: "Storage readers return [] on read/JSON failure or skip malformed JSONL lines. Page 1 then succeeds with messages=[] and total_pages=1; this method's try cannot observe those failures."
    suggested_action: "Distinguish missing, empty, partial/corrupt, and I/O failure in the load result."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Direct coverage exercises only oversized-record sanitization."
    evidence: "The sole direct test found covers a large restorable record. It omits path escape/create behavior, invalid types, missing/corrupt/empty history, filtering, newest-first paging, page bounds, and both caller contracts."
    suggested_action: "Test containment, validation, paging/filter order, load failures, and both handlers."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.get_conversation_history`

## Actual Role

Loads all persisted history, keeps restorable records, orders newest first, selects 20 records, and sanitizes them for ordinary or streaming delivery.

## Key Signals

- Input: Nonblank string ID and positive integer page; ID containment is unchecked.
- Output: `{messages, total_pages, page_idx}` or `None`; empty/corrupt input can yield an empty success.
- Side effects: Read helpers can create the derived directory.
- Main risks: Filesystem escape, unbounded full-history reads per page, and lost corruption/I/O error semantics.
- Related flow/tests: `agentserver-session-lifecycle`; one direct payload-size test, with pagination and boundary contracts uncovered.

## Detail Index

- Detail docs pending.
