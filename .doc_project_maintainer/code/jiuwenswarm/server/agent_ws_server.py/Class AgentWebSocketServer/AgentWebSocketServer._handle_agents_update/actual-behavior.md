---
symbol: AgentWebSocketServer._handle_agents_update
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_agents_update`

## Actual Role

Updates the active custom-agent definition selected by name and request-provided workspace. It silently filters request fields into UpdateAgentParams, can replace when_to_use/prompt with one LLM generation, synchronously overwrites the resolved Markdown file, globally reloads active agents, and returns the persisted definition plus generated/applied/reload_error status.

## Key Signals

- Caller: `_handle_message` dispatches `ReqMethod.AGENTS_UPDATE`; Web and TUI forward it.
- Inputs: Name, optional workspace/generation, and description/prompt/model/tool/policy/memory/iteration/skill fields; unknown fields are silently dropped.
- Side effects: Optional LLM request, synchronous persistent host-file overwrite, and global active-agent reload.
- Failure model: Validation/generation/write failure returns `ok=false`; reload failure preserves the write and returns `ok=true, applied=false`, which the current TUI success branch ignores.
- Tests/flow: Service tests cover basic update and builtin/nonexistent rejection only; no handler, forwarding, generation, path-boundary, no-op, concurrency, or degraded-reload contract test was found. No tests were run during this re-audit.

## Detail Index

- Detail docs pending.
