---
symbol: AgentWebSocketServer._handle_session_rename
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_session_rename audit evidence

## ISSUE-001: Unvalidated target session ids can escape the session storage root.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Current handler passes request.params directly to apply_session_rename. That helper lets params.session_id override request.session_id and only str/strip-normalizes it; _read_metadata and _metadata_file compose get_agent_sessions_dir() / session_id / metadata.json without id validation or resolved-path containment.
- Suggested action: Validate the effective id in the shared helper, reject absolute/traversal paths, and require the resolved metadata path to remain below the sessions root.

## ISSUE-002: A successful response does not guarantee that the renamed title reached durable metadata.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: For set/clear, apply_session_rename calls update_session_metadata, which updates _METADATA_CACHE and enqueues a background write, then immediately rereads the cache for the success payload. The worker logs write failure without changing the already-sent result.
- Suggested action: Define whether success means cache acceptance or durability; for durability, await or acknowledge the metadata write and test failures.

## ISSUE-003: Missing direct rename-handler and helper tests.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: Current repository search found no test reference to session.rename, apply_session_rename, _handle_session_rename, or previous_title. test_session_metadata covers lower-level init/update/get and queued persistence only.
- Suggested action: Add focused tests for query, set, clear, missing session_id/BAD_REQUEST, non-dict params, and WebSocket response encoding for _handle_session_rename.
