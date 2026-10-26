---
symbol: AgentWebSocketServer._handle_session_switch
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_switch`

## Actual Role

Handles the team-only `session.switch` preparation RPC. It derives a target from params/request identity, accepts caller-declared team variants, asks the channel TeamManager to enforce distributed single-session policy, then sends one locked E2A response. It does not validate persisted target identity, load/activate the target, or persist an active-session selection; local runtime preparation is a no-op.

## Key Signals

- Input: `params.session_id` wins, then `request.session_id`; `params.mode` or `params.team` controls team-mode acceptance.
- Output: Reports `{session_id, mode: "team", switched: true}` after preparation; validation failures use `BAD_REQUEST` or `UNSUPPORTED_MODE`.
- Main side effects: Calls `get_team_manager(channel_id).prepare_session_switch(target, reason="session.switch: ")`.
- Main risk: an unverified target can stop valid distributed runtimes, while success claims a completed switch and collapses all accepted team variants to `team`.
- Test evidence: handler tests cover team success/non-team rejection; TeamManager tests cover distributed cleanup/local no-op. No tests were run for this documentation-only re-audit.
- Related flow: `agentserver-session-lifecycle` correctly classifies switch as runtime work but remains partial and does not resolve target validation or activation semantics.

## Detail Index

- Detail docs pending.
