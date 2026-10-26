---
symbol: AgentWebSocketServer._handle_agents_create
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_agents_create`

## Actual Role

Handles unary `agents.create` by accepting optional workspace/generate controls, optionally replacing request `when_to_use` and `prompt` with best-effort LLM output, silently filtering remaining fields into `CreateAgentParams`, and synchronously creating/overwriting a Markdown definition. It then enables the name in shared config and globally reloads agents; reload failure is softened to `ok=true, applied=false`, while earlier failures return error text, and the full created dataclass is sent under `send_lock`.

## Key Signals

- Input: Agent definition fields plus optional `workspace_dir` and `generate`.
- Output: Full created agent plus generated/applied flags and optional reload error; success can mean durable file/config changed but runtime application failed.
- Main side effects: Optional LLM call, arbitrary-workspace directory/file creation or overwrite, global config mutation, broad runtime reload, logs, and WebSocket send.
- Main risk: Partial or destructive durable state can be reported as a successful creation and affect unrelated runtimes.
- Related tests: Service tests cover create validation/file/overwrite behavior, but no handler orchestration, LLM, workspace-boundary, partial-failure, rollback, global-reload, or wire test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
