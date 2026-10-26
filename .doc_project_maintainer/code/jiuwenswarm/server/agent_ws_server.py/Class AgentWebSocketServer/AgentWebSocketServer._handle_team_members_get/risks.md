---
symbol: AgentWebSocketServer._handle_team_members_get
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_team_members_get audit evidence

## ISSUE-001: Zero seats, unavailable runtime, and query failure share one success shape.

- Dimension: `output_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, query_team_human_members_for_join converts a missing DB path, DB initialization/query failure, team-name miss, and a real zero-human-seat result into an empty list; this handler returns all of them as ok=True, and Gateway /join maps every empty list to the same not-ready rejection.
- Suggested action: Add ready/status/error fields and reserve members=[] for successful zero seats.

## ISSUE-002: Session/team identity is derived from unchecked caller strings.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, params.session_id overrides request.session_id without type or non-empty validation. The helper stringifies and lossy-sanitizes it into a team-name suffix; a blank session bypasses suffix binding entirely, and different invalid IDs can collapse to the same suffix.
- Suggested action: Require a canonical non-empty session ID and resolve its authoritative team identity before querying storage.

## ISSUE-003: Live member discovery has no local timeout.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: At HEAD 39feee89, the handler directly awaits query_team_human_members_for_join with no local bound; the helper may initialize the shared SQLite database and await get_team_members, so a stalled storage backend holds this AgentServer request until an outer timeout or disconnect.
- Suggested action: Bound discovery and return an explicit unavailable status.

## ISSUE-004: The obsolete cross-channel monitor fallback was removed.

- Dimension: `implementation_soundness`
- Severity: `low`
- Status: `fixed`
- Evidence: At HEAD 39feee89 (commit 51ac4cb1), the handler delegates to a storage-backed helper that queries team.db and no longer iterates channel managers or depends on a live monitor, eliminating the former dead singleton fallback.
- Suggested action: Retain coverage proving member lookup works while no monitor runtime is active.

## ISSUE-005: The advertised resolved team name is only the caller's value echoed back.

- Dimension: `implementation_soundness`
- Severity: `high`
- Status: `open`
- Evidence: At HEAD 39feee89, query_team_human_members_for_join derives full_team_name for the DB query but returns (members, team_name), where team_name is the request argument. This handler forwards it as resolved_team_name; Gateway then compares it with the same expected_team_name it originally sent, so every non-empty lookup makes the response-level consistency check tautologically pass rather than reporting authoritative session-to-team identity.
- Suggested action: Resolve and return the stored/session-authoritative team name, and compare it to the requested name independently of the DB lookup key.

## ISSUE-006: Handler tests mock away the identity and storage behavior that carries the main risk.

- Dimension: `test_coverage`
- Severity: `medium`
- Status: `open`
- Evidence: tests/unit_tests/agentserver/test_team_members_get.py verifies only response passthrough with query_team_human_members_for_join stubbed. No direct helper/integration test covers DB initialization, canonical identity resolution, blank or malformed session IDs, mismatched team names, storage failure, or timeout; gateway tests exercise TeamMemberLookup in isolation.
- Suggested action: Add integrated helper/handler/Gateway tests using authoritative session metadata and a temporary team database, including mismatch and failure cases.
