---
last_updated: 2026-07-07
status: partial
confidence: inferred
---

# Project Overview

JiuwenSwarm is a multi-agent application runtime. It packages a standalone AgentServer, a Gateway process, channel adapters, web/TUI/CLI frontends, distributed team coordination, skill and memory systems, sandbox support through jiuwenbox, and automation/scheduler support.

The primary runtime boundary is Gateway -> AgentServer. Gateway and channels normalize user, platform, and protocol messages into internal E2A envelopes. AgentServer accepts those envelopes over WebSocket, resolves sessions and modes, invokes the correct agent adapter through `AgentManager`, streams or returns E2A responses, and can also push agent-originated events back to Gateway.

This first artifact build prioritizes AgentServer because it is the central runtime boundary. Full repository symbol coverage is not complete yet.

## Entrypoints

- `pyproject.toml`: package metadata and console scripts.
- `jiuwenswarm.server.app_agentserver:main`: standalone AgentServer process.
- `jiuwenswarm.gateway.app_gateway:main`: Gateway process that connects to AgentServer.
- `jiuwenswarm.app:main`: combined app entrypoint.
- `jiuwenswarm.channels.web.app_web:main`: web frontend service entrypoint.
- `jiuwenswarm.acp.cli:main`: ACP CLI path.

## First-Build Evidence

- Source inventory command ran on commit `af779aa6742969e46005a2a94f49d42d7a3b443a`.
- Inventory found 1236 source files and 14746 required repository symbols.
- Default product/runtime health audit scope contains 8737 symbols.
- `agent_ws_server.py` alone contains 152 symbols and needs sliced follow-up.
