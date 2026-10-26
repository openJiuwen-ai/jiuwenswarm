---
symbol: AgentWebSocketServer._handle_session_create
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_create audit evidence

## ISSUE-001: Untrusted explicit session IDs become authoritative filesystem identities.

- Dimension: `boundary_safety`
- Severity: `critical`
- Status: `open`
- Evidence: Current code only strips a string before AgentManager returns every nonempty explicit value unchanged. The TUI /new command accepts arbitrary text, while session_history._session_dir, session_metadata._metadata_file, TodoWrite and diff helpers later join session_id below the sessions root without resolved-path containment; '..' or an absolute ID can therefore escape that root.
- Suggested action: Centralize strict ID syntax/length validation and enforce resolved-path containment at each filesystem boundary.

## ISSUE-002: Create neither persists nor uniquely reserves a session.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: Current AgentManager.create_session only echoes a nonempty ID, generates acp_<8 hex> for ACP, or returns 'default' for another channel. It creates no directory, metadata, history, checkpoint, reservation, or duplicate check; the separate Web/TUI gateway-local handlers do create a directory and metadata, but this handler still reports ok=true from the ID-only path.
- Suggested action: Use one transport-independent service to validate/reserve identity, persist metadata atomically, and report conflicts.

## ISSUE-003: Team creation can stop distributed runtimes before success is observable.

- Dimension: `side_effects`
- Severity: `high`
- Status: `open`
- Evidence: For resolved mode 'team', current code awaits TeamManager.prepare_session_switch before encoding or sending success. Distributed preparation can stop stale sessions under its switch lock; a later preparation/encoding/send failure is caught after that mutation and attempts a second error send on the same socket, with no rollback or applied-state field.
- Suggested action: Separate creation from switching; make switching recoverable and classify send failures before retrying.

## ISSUE-004: Tests cover mocked success and one successful team switch only.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: The three direct tests use FakeAgentManager/FakeTeamManager to assert generated/explicit IDs and one team-switch call. A separate routing test replaces this handler entirely. No test covers the real manager's default fallback, hostile/duplicate IDs, persistence, containment, partial switch failure, encode failure, or send failure.
- Suggested action: Add real-manager contract, adversarial ID, persistence, switch-failure, and transport-failure tests.
