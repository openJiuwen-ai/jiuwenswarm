---
symbol: AgentWebSocketServer._handle_cancel
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_cancel audit evidence

## ISSUE-001: Missing team mode params could make team cancellation cleanup run after generic interrupt handling.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `fixed`
- Evidence: Current _handle_message lines 1624-1636 cancel the session-keyed stream task before calling _handle_cancel for cancel/supplement intents, so the pre-existing team stream is no longer dependent on late mode-based cleanup inside this method. Current _handle_cancel contains no post-process team cleanup branch.
- Suggested action: Retain the pre-dispatch session-task cancellation and its disconnect/manual cancel regression coverage.

## ISSUE-002: Coverage exercises disconnect no-create behavior but not runtime selection.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Eight focused cancel cases in test_agent_ws_connection_close.py cover existing fake-agent dispatch, allow_create=False with no agent, send/stream-cleanup failures, trusted disconnect source, and manual cancel retention. None distinguishes multiple cached modes, asserts default creation, or supplies malformed params directly.
- Suggested action: Add focused tests for exact-mode reuse, ambiguous/missing-mode selection, default creation, malformed params, and encoded response send.

## ISSUE-003: Cache fallback can deliver cancellation to the wrong mode runtime.

- Dimension: `boundary_safety`
- Severity: `medium`
- Status: `open`
- Evidence: Lines 1797-1814 first look up requested mode/project, then retry get_agent_nowait(channel_id, project_dir=project_dir) without mode. AgentManager.get_agent_nowait returns the first matching channel/project agent, so multiple cached modes or a stale mode hint can select another runtime; session_id is not part of selection.
- Suggested action: Resolve cancellation by session-to-runtime ownership, or reject ambiguous fallback instead of selecting the first cached mode.

## ISSUE-004: A normal cancel can create a new agent when no runtime exists.

- Dimension: `side_effects`
- Severity: `medium`
- Status: `open`
- Evidence: With default allow_create=True, two cache misses reach AgentManager.get_agent at lines 1836-1849, which auto-creates a runtime before process_message handles the interrupt. Only the client-disconnect caller passes allow_create=False and receives the synthetic no-runtime acknowledgement.
- Suggested action: Default cancellation to no-create and return the existing acknowledgement unless a caller explicitly requires runtime creation.
