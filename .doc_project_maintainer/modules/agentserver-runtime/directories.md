# AgentServer Runtime Directories

- `jiuwenswarm/server`: runtime entrypoint, WebSocket server, runtime services, sandbox, hooks, gateway push.
- `jiuwenswarm/server/runtime`: agent manager, adapters, session persistence helpers, skills, A2UI, proactive adapter.
- `jiuwenswarm/server/gateway_push`: server-originated push transport and E2A wire encoding.
- `jiuwenswarm/gateway/routing`: Gateway WebSocket client evidence for AgentServer protocol.
- `tests/unit_tests/agentserver`: direct unit behavior for AgentServer dispatch and handlers.
- `tests/unit/agentserver`: additional focused AgentServer tests.
