---
id: agent-harness
name: Agent Harness
confidence: inferred
last_updated: 2026-08-01
read_when: "Working on agent adapters, rails, tools, team runtime, skills, memory, or AutoHarness execution."
---

# Agent Harness

## Responsibility

Provides the agent execution machinery behind AgentServer: adapters, rails, memory, tools, skill management, code mode, team orchestration, distributed members, and AutoHarness scheduler/package workflows.

## Boundaries

- Owns: adapter behavior after AgentServer dispatch, in-agent state, team manager behavior, skill and memory runtime, tool rails.
- Does not own: WebSocket protocol parsing or Gateway channel output.

## Current Evidence

AgentServer calls this module family through `AgentManager`, adapter methods such as `process_message` and `process_message_stream`, team manager helpers, scheduler services, ACP output callbacks, and sandbox runtime patches.

`JiuWenSwarmDeepAdapter.prepare_session` now creates the session-scoped child, starts the real OpenJiuwen interaction, and calls the child's public `configure_session_runtime` boundary without attaching output or sending input. Request-scoped permissions, paths, ACP capabilities, output attachment, and input remain on the real chat path.

Runtime-state Git probes and `.agent_history` Git-ignore checks no longer run synchronously on the AgentServer loop. Runtime-state writes are coalesced per adapter and limited to two worker threads, so diagnostic repository inspection cannot hold up `chat.send` dispatch.

OpenJiuwen tool/resource registration is process-global, so foreground and speculative preparation share one initialization lock. MemoryRail avoids a full reindex on first registration and singleflights reindex by normalized workspace plus embedding fingerprint when configuration actually changes.

## Related Flows

- `agentserver-command-mcp`: adapter MCP reconciliation after persisted config changes.
- `agentserver-sandbox-runtime`: Agent recreation, sys-operation policy, and runtime hot patching.
- `agentserver-plan-mode-exit`: plan approval rails and checkpoint-backed mode restoration.
- `agentserver-schedule-auto-harness`: durable scheduler tasks and autonomous Harness execution.
- `session-prewarm-allocation`: session-stable DeepAgent preparation and request-bound runtime binding.

## Pending

This module has not been fully analyzed. Next high-value slices are `server/runtime/agent_adapter/interface_deep.py`, `server/runtime/skill/skill_manager.py`, and `agents/harness/team`.
