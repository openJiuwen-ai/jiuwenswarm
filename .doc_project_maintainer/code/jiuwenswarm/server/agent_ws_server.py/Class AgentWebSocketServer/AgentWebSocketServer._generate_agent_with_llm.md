---
symbol: AgentWebSocketServer._generate_agent_with_llm
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_generate_agent_with_llm(self, name: str, description: str) -> tuple[str, str] | None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
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
    dimension: error_handling
    severity: high
    status: open
    summary: "The documented fallback contract does not cover several realistic failures."
    evidence: "_resolve_model and UserMessage import run outside the try block. Non-object JSON raises on data.get and non-string fields on strip; these escape to agents.create/update instead of returning None for template fallback."
    suggested_action: "Put setup, invocation, and decoding inside the fallback boundary; require a mapping with two bounded strings."
  - id: ISSUE-002
    dimension: performance_risk
    severity: medium
    status: open
    summary: "The external model call has no server-side deadline."
    evidence: "model.invoke has no timeout. TUI callers wait 60 seconds, so generation can outlive the client wait and create/update can later continue toward persistence."
    suggested_action: "Apply a bounded timeout and cancellation; do not persist after request expiry."
  - id: ISSUE-003
    dimension: observability
    severity: medium
    status: open
    summary: "Failure logs can disclose generated instructions and user-derived content."
    evidence: "Parse warnings log response text and incomplete-response logging emits the full decoded object, potentially including systemPrompt and user-derived content."
    suggested_action: "Log structured reasons and bounded metadata, not raw model output."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "Tests duplicate parser code instead of exercising the method."
    evidence: "TestAgentLLMGeneration copies parsing logic and never imports AgentWebSocketServer. It misses drift, setup failures, typed validation, timeout/cancellation, logging, and caller fallback."
    suggested_action: "Test the real method with a fake model and add create/update fallback and cancellation contracts."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._generate_agent_with_llm`

## Actual Role

Resolves the default model, sends one user-role prompt containing the agent name and description, and extracts `whenToUse` plus `systemPrompt`. It initializes the shared model cache and returns `None` for only some failures.

## Key Signals

- Input: Unbounded agent name and description in one model message.
- Output: Two generated strings or `None`; some malformed outputs and setup failures raise.
- Main side effects: Initializes shared model cache, calls an external model, and logs failures.
- Main risk: Malformed or slow responses can defeat fallback or outlive the client request before persistence.
- Related tests/flow: Only parser-copy tests; no direct test or agent-generation flow doc, so flow remains pending.

## Detail Index

- Detail docs pending.
