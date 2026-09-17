---
symbol: AgentWebSocketServer._handle_session_create
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_create audit evidence

## ISSUE-001: Explicit session IDs lacked a safe compatibility boundary.

- Dimension: `boundary_safety`
- Severity: `critical`
- Status: `fixed`
- Evidence: Normal `session.create` rejects explicit IDs outside TUI. Its TUI-only compatibility branch requires `sanitize_session_id(id) == id`, caps length at 128, rejects cross-channel ownership, and retains AgentServer as the metadata writer.
- Remaining note: Existing-session handlers outside this compatibility path still require their own containment review.

## ISSUE-002: Creation did not persist or reserve a session.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `fixed`
- Evidence: Normal creation claims an AgentServer-owned ID and synchronously initializes metadata. TUI explicit-ID creation uses a per-ID asyncio lock and idempotently creates or reuses metadata before success.
- Remaining note: `create_token` idempotency and explicit-ID locks are process-local.

## ISSUE-003: Team creation can stop distributed runtimes before success is observable.

- Dimension: `side_effects`
- Severity: `high`
- Status: `open`
- Evidence: For resolved mode 'team', current code awaits TeamManager.prepare_session_switch before encoding or sending success. Distributed preparation can stop stale sessions under its switch lock; a later preparation/encoding/send failure is caught after that mutation and attempts a second error send on the same socket, with no rollback or applied-state field.
- Suggested action: Separate creation from switching; make switching recoverable and classify send failures before retrying.

## ISSUE-004: Creation compatibility boundary tests were incomplete.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `fixed`
- Evidence: Direct cases now cover allocation/persistence, explicit-ID rejection outside TUI, TUI explicit-ID creation, concurrent idempotency, invalid/cross-channel IDs, stable project binding, warm bypass, KVC ack ordering, and Team preparation. Related suites passed with prewarming both disabled and enabled.
- Remaining note: Transport send failure and Team preparation rollback still lack direct coverage.
