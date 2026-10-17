---
last_updated: 2026-09-07
status: partial
confidence: inferred
---

# Architecture

## Runtime Shape

Channels and frontends feed Gateway. Gateway uses an `AgentServerClient` implementation to connect to the AgentServer WebSocket endpoint. AgentServer decodes E2A or legacy request payloads into `AgentRequest`, dispatches special RPC methods locally, and delegates chat/runtime work to `AgentManager` and the selected agent adapter.

The first Session-unification slice adds a second, deliberately narrow reference entry. `InProcessRuntimeClient` owns an `AgentRuntime`, which owns `RuntimeSessionCoordinator`, `SessionWorkScheduler`, and `SessionExecutionRegistry`. Process CLI Work/Code Normal use this managed path; AgentServer, Plan, Team, and background work remain on the legacy path until staged adaptation. Durable metadata/history are unchanged.

```text
Process CLI -> InProcessRuntimeClient -> AgentRuntime
  -> RuntimeSessionCoordinator -> Scheduler/Registry
  -> migration Executor -> existing facade/adapter -> RuntimeEvent
```

```text
Channel or frontend
  -> Gateway message handling and routing
  -> WebSocketAgentServerClient
  -> AgentWebSocketServer
  -> AgentManager
  -> agent adapter, rails, tools, team, skill, memory, sandbox
  -> E2A response or stream chunks
  -> Gateway
  -> channel/frontend output
```

When explicitly enabled per request, both Work and Code adapters mount the same eternal-conversation Rail. The Rail writes a complete hash-chained Raw History, while a Session-owned coordinator—not the disposable channel Adapter—runs the semantic Extractor and the Pending-to-Built Builder. Snapshot replacement is allowed only at a task boundary where the published covered cursor equals the latest completed requested cursor; ContextProcessor output recorded after its changes remains authoritative foreground evidence.

## AgentServer Responsibilities

- Owns WebSocket server lifecycle and connection cleanup.
- Converts E2A/legacy wire payloads into runtime requests.
- Dispatches control methods for sessions, history, team snapshots, slash commands, MCP, sandbox, agents, extensions, harness packages, schedule actions, and ACP tool responses.
- Tracks stream tasks by session so cancel/supplement interrupts can stop live work.
- Sends stream heartbeats while long agent streams are running.
- Provides server push for agent-originated events, including ACP output and compression state updates.
- Starts optional jiuwenbox sandbox runtime only when config explicitly requests internal sandbox startup.
- Constructs `AgentRuntime` without a Session mode override and therefore remains legacy during the first unification slice.

## Coverage Note

This architecture doc is an initial map. It does not yet cover every subsystem in `jiuwenswarm/agents`, `jiuwenswarm/channels`, `jiuwenbox`, or frontend packages.
