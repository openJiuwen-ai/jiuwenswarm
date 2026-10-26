---
symbol: AgentWebSocketServer._handle_command_diff
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_diff`

## Actual Role

Handles `command.diff` by defaulting the session, resolving a request-provided project path, and running turn-history and current-git diff reads concurrently in worker threads. It returns `type=list`, every changed `turn`, and optional `gitDiff`; the shared send helper replaces an oversized final wire payload with a bounded error.

## Key Signals

- Input: `request_id`, `channel_id`, optional `session_id`, and project metadata from params/metadata.
- Output: One WebSocket response with the diff list payload, `ok=false` with a raw exception string, or the send helper's bounded `response_too_large` fallback.
- Main side effects: Reads history/git state, can create session directories through read helpers, logs, and sends a WebSocket frame.
- Main risk: Client-controlled session/project paths drive filesystem and git reads, while all session turn diffs are computed before the final send budget is enforced.
- Related tests: One direct empty-default handler test and separate tracked git-diff service tests exist; path/error/dispatch/frontend and turn-diff boundary cases are missing. No tests were run during this re-audit.

## Detail Index

- Detail docs pending.
