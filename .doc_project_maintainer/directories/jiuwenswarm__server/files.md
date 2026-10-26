# `jiuwenswarm/server` Files

- `app_agentserver.py`: runtime process setup, env/logging/safety patches, extension load, WebSocket server lifecycle, proactive adapter, teammate bootstrap, signal shutdown.
- `agent_ws_server.py`: broad AgentServer dispatch and runtime command surface.
- `gateway_push/wire.py`: converts push message dicts into E2A response or chunk wire frames and marks them as server push.
- `gateway_push/transport.py`: protocol plus default WebSocket transport through `AgentWebSocketServer.get_instance().send_push`.
- `runtime/*`: agent manager, adapters, sessions, skills, A2UI, proactive, and tenant pool.
- `hooks/*`: hook execution and user-hook rail integration.
- `sandbox/*`: jiuwenbox sandbox runner boundary.
- `utils/*`: diff, stream, and miscellaneous server utilities.
