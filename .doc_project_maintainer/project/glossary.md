# Glossary

- AgentServer: Standalone process that hosts agent runtime logic and accepts Gateway WebSocket requests.
- Gateway: Process that accepts channels/frontends and forwards normalized requests to AgentServer.
- E2A: Internal Everything-to-Agent envelope and response format.
- ACP: Agent Client Protocol integration path.
- A2A: Agent-to-Agent external protocol integration path.
- AgentRequest: Legacy/in-process request shape consumed by AgentServer and adapters.
- AgentManager: Runtime owner for channel-specific agent instances, sessions, config reload, and initialization.
- Server push: AgentServer-originated downstream event sent to Gateway without a matching awaited RPC response.
- Default health audit: Project Maintainer audit scope for runtime and library source symbols.
- Repository coverage only: Project Maintainer coverage for tests, scripts, tooling, and metadata outside default runtime health audit.
