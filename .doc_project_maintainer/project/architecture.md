---
last_updated: 2026-07-07
status: partial
confidence: inferred
---

# Architecture

## Runtime Shape

Channels and frontends feed Gateway. Gateway uses an `AgentServerClient` implementation to connect to the AgentServer WebSocket endpoint. AgentServer decodes E2A or legacy request payloads into `AgentRequest`, dispatches special RPC methods locally, and delegates chat/runtime work to `AgentManager` and the selected agent adapter.

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

## AgentServer Responsibilities

- Owns WebSocket server lifecycle and connection cleanup.
- Converts E2A/legacy wire payloads into runtime requests.
- Dispatches control methods for sessions, history, team snapshots, slash commands, MCP, sandbox, agents, extensions, harness packages, schedule actions, and ACP tool responses.
- Tracks stream tasks by session so cancel/supplement interrupts can stop live work.
- Sends stream heartbeats while long agent streams are running.
- Provides server push for agent-originated events, including ACP output and compression state updates.
- Starts optional jiuwenbox sandbox runtime only when config explicitly requests internal sandbox startup.

## Coverage Note

This architecture doc is an initial map. It does not yet cover every subsystem in `jiuwenswarm/agents`, `jiuwenswarm/channels`, `jiuwenbox`, or frontend packages.
