---
symbol: resolve_agent_request_mode
kind: function
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
signature: "resolve_agent_request_mode(raw_mode: Any, *, work_mode: Any = None) -> tuple[str, str | None, str]"
health:
  overall: unknown
audit:
  status: unaudited
confidence: confirmed
---

# resolve_agent_request_mode

## Actual Role

Parses request mode into AgentManager mode, sub-mode, and canonical metadata value. For eligible single-Agent identities, an optional final `work_mode` overrides contradictory legacy input: work becomes `(agent, None, agent)` and code becomes `(code, normal, code.normal)`; plan/team variants retain their specialized identity.

## Key Signals

- Input: raw request mode and optional authoritative work mode.
- Output: normalized manager mode, sub-mode, and canonical mode.
- Side effects: none.
- Main risk: callers that omit authoritative work mode retain legacy mode-only behavior.
- Related tests: `test_resolve_agent_request_mode_accepts_primary_and_dotted_modes` and `test_resolve_agent_request_mode_aligns_single_agent_with_work_mode`.

Health remains unaudited; see the build plan.
