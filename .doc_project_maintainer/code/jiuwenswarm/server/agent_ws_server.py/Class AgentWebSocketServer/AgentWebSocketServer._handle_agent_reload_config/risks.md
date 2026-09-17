---
symbol: AgentWebSocketServer._handle_agent_reload_config
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_agent_reload_config audit evidence

## ISSUE-001: Malformed reload_scopes can unexpectedly trigger a global reload.

- Dimension: `input_contract`
- Severity: `high`
- Status: `open`
- Evidence: Lines 5864-5869 collapse every non-list value and every list with no non-empty strings to the same empty set used for an omitted scope. Lines 5877 and 5886 interpret that set as reload all AgentManager and proactive domains; without a target, AgentManager traverses every cached channel.
- Suggested action: Validate the container and known string values, and distinguish an omitted scope from an explicitly empty or invalid scope.

## ISSUE-002: Unknown scopes are accepted as a successful no-op.

- Dimension: `output_contract`
- Severity: `high`
- Status: `open`
- Evidence: A non-empty typo or otherwise unsupported scope intersects neither the AgentManager set at line 5876 nor the proactive set at line 5886. The handler then executes neither reload branch but lines 5900-5905 still return ok=true and reloaded=true, which the Gateway treats as applied.
- Suggested action: Define the accepted scope vocabulary, reject unsupported values, and return per-domain applied/skipped/error results.

## ISSUE-003: Proactive reload ignores the request's authoritative config snapshot.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: Lines 5879-5883 pass params.config to AgentManager, but lines 5888-5896 re-read the server's local config for proactive fields and invoke a builder that also resolves model state independently. The Gateway explicitly sends its post-save full snapshot for AgentServer to prefer, so the two branches can apply different versions.
- Suggested action: Validate one effective request snapshot and use it, including its model settings, for every selected reload domain.

## ISSUE-004: Partial reload failures can still produce reloaded=true.

- Dimension: `error_handling`
- Severity: `high`
- Status: `open`
- Evidence: AgentManager catches team evolution update failures and returns None; ProactiveEngine's rebuild path catches build errors and also treats a None build as a silent non-replacement. The handler receives no outcome from either path and lines 5900-5905 report complete success.
- Suggested action: Return structured component outcomes and mark any selected but unapplied domain degraded or failed.

## ISSUE-005: Valid scope routing is tested, but failure and malformed contracts are not.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Direct handler tests cover an unscoped targeted reload, web_ui skipping, and proactive-only routing; manager tests cover targeting, dedupe, and retry after a swallowed team failure. No test covers invalid scope shapes/values, empty scopes, request-vs-local snapshot divergence, process-global env effects, rebuild failure/None, or truthful response detail.
- Suggested action: Add malformed/unsupported scope, global-env boundary, partial-failure, and Gateway snapshot integration tests.

## ISSUE-006: A targeted reload still applies request environment overrides process-wide.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: The handler forwards env together with target_channel_id/target_session_id at lines 5879-5883. AgentManager.reload_agents_config writes every provided key into os.environ under its global reload lock before resolving the target channel/session, so a request presented as targeted can change configuration observed by all channels and unrelated server code.
- Suggested action: Separate process-global environment updates from channel/session reload targeting; require an explicit global-env operation or keep overrides scoped in immutable per-agent configuration.
