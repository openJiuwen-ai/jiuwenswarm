---
symbol: AgentWebSocketServer._handle_team_delete
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_team_delete`

## Actual Role

Handles unary `team.delete` by validating team mode/name, requiring the persistent checkpointer, and locating every session whose metadata matches the team. It then sequentially stops those session runtimes, asks OpenJiuwen Runner to force-delete persistent team/session state, removes each local session directory and cache entry, and sends a success response; validation, unavailable-checkpointer, and no-session branches send stable failures, while unexpected exceptions fall through to `_handle_message`.

## Key Signals

- Input: `AgentRequest.params` must provide a nonblank `team_name` and satisfy `is_team_params`.
- Output: Sends one E2A response; local branches use `BAD_REQUEST`, `UNSUPPORTED_MODE`, `CHECKPOINT_UNAVAILABLE`, or `NOT_FOUND`, while success returns `team_name`, all matched `session_ids`, and `deleted: true`.
- Main side effects: Sequentially stops shared team runtimes, deletes persistent Runner state, removes local session trees, clears per-session metadata cache, and writes a WebSocket frame.
- Main risk: Destructive cleanup spans runtime, checkpoint/database, filesystem, and metadata cache without atomicity, and some partial failures are reported as success.
- Related tests: Four direct `test_agentserver_acp.py` cases cover success, missing name, non-team mode, and checkpointer failure; selector behavior has adjacent coverage. No partial/lower-level failure case was found, and tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
