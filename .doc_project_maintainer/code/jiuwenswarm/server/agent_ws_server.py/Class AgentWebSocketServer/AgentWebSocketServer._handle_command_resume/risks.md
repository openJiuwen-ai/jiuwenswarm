---
symbol: AgentWebSocketServer._handle_command_resume
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_command_resume audit evidence

## ISSUE-001: The resume command reports success without resuming anything.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: Current method has no session/history/agent dependency or state transition. Its normal branch always constructs ok=true, resumed=true, and the literal preview 'Mock resumed conversation'.
- Suggested action: Implement the real session transition and history validation, or remove/deprecate command.resume and return an explicit unsupported error.

## ISSUE-002: Missing and invalid session selectors fabricate a successful mock session.

- Dimension: `input_contract`
- Severity: `high`
- Status: `open`
- Evidence: Missing, blank, or non-string params.query becomes session_id='sess_mock_resume'. A nonblank string is returned verbatim (not stripped) without existence, ownership, channel, or project validation.
- Suggested action: Require a normalized session_id, validate it through the canonical session service, and return structured not-found/conflict/forbidden errors.

## ISSUE-003: The routed public endpoint has drifted away from the real TUI resume flow.

- Dimension: `dependency_coupling`
- Severity: `high`
- Status: `open`
- Evidence: COMMAND_RESUME remains in ReqMethod and AgentServer dispatch/forwarding, while the documented live session flow performs selection and history restoration through session/history APIs; this method invokes none of those services.
- Suggested action: Choose one canonical resume contract, migrate clients, then remove the dead mock route and forwarding entry.

## ISSUE-004: The only direct test codifies the mock response.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: The only direct test, test_handle_command_resume_returns_mock_session, asserts resumed=true and the mock preview. It does not verify existing/missing sessions, transition, history restoration, authorization, malformed input, or live-flow parity.
- Suggested action: Replace the mock assertion with behavioral integration tests against the canonical session/history services and invalid-input cases.
