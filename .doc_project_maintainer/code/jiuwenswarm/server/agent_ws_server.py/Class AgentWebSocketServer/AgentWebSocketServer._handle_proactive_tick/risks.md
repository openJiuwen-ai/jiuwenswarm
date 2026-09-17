---
symbol: AgentWebSocketServer._handle_proactive_tick
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_proactive_tick audit evidence

## ISSUE-001: Missing direct coverage for the websocket adapter and proactive cron branch.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: ProactiveEngine flow tests cover delivery, cooldown, quota, and busy-session outcomes, but no direct handler cases for uninitialized, success, skip, exception, or target pass-through, and no Gateway Cron proactive envelope/result-mapping test, were found.
- Suggested action: Add fake-engine and fake-websocket tests for uninitialized, success, skipped, exception, target_channel pass-through, and scheduler envelope mapping.

## ISSUE-002: params and target_channel are accepted implicitly from an Any boundary.

- Dimension: `input_contract`
- Severity: `low`
- Status: `open`
- Evidence: Canonical E2A conversion coerces params to dict, but legacy _payload_to_request preserves raw params; lines 3049-3050 use request.params or {}, call .get without a dict guard, and forward target_channel unchanged. A truthy non-dict becomes a local error, while a wrong target type can reach the engine and be swallowed as False.
- Suggested action: Guard request.params as a dict and normalize target_channel with str.strip() to a non-empty string or None before calling the engine.

## ISSUE-003: Engine failures are reported to Cron as benign no-recommendation skips.

- Dimension: `observability`
- Severity: `medium`
- Status: `open`
- Evidence: ProactiveEngine.tick_now catches ordinary exceptions and returns False; lines 3051-3065 map every False to ok=true/no_recommendation, possibly appending an older last_tick_at, and Gateway Cron lines 574-579 record that response as skipped.
- Suggested action: Return a structured tick outcome or propagate engine failures so the handler and scheduler can distinguish failed from skipped ticks.
