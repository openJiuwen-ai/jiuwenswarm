---
id: agentserver-runtime
name: AgentServer Runtime
confidence: confirmed
last_updated: 2026-08-01
read_when: "Working on AgentServer startup, Gateway WebSocket handling, sessions, commands, server push, ACP, scheduler, sandbox, or runtime services."
---

# AgentServer Runtime

## Responsibility

Hosts the standalone AgentServer process and the WebSocket RPC surface used by Gateway. It decodes E2A or legacy request payloads, resolves channel/session/mode context, routes special control methods, invokes agent adapters through `AgentManager`, streams responses, and pushes agent-originated events back to Gateway.

## Boundaries

- Owns: `jiuwenswarm.server.app_agentserver`, `AgentWebSocketServer`, server runtime helpers, gateway push wire encoding, session command handlers, runtime command handlers, scheduler entrypoints, sandbox command boundary.
- Does not own: channel-specific ingestion, frontend rendering, most agent/tool implementation internals, and external protocol semantics before Gateway normalization.

## Entry Points

- `jiuwenswarm/server/app_agentserver.py`: standalone process startup, extension loading, WebSocket server startup, proactive engine, teammate bootstrap daemon, shutdown.
- `jiuwenswarm/server/agent_ws_server.py`: WebSocket server, E2A/legacy request parsing, method dispatch, session/command handlers, stream/cancel logic, server push.
- `jiuwenswarm/server/gateway_push/wire.py`: converts server-originated push messages into E2A response wire frames.
- `jiuwenswarm/server/runtime/agent_manager.py`: creates, initializes, reloads, and retrieves agent instances.
- `jiuwenswarm/server/runtime/agent_warm_pool.py`: reconciles and atomically claims session-bound READY DeepAgents.
- `jiuwenswarm/server/runtime/proactive_adapter.py`: attaches proactive recommendation engine to the AgentServer instance.

## Related Flows

- `gateway-agentserver-e2a-chat`: Gateway request -> AgentServer dispatch -> agent response.
- `agentserver-session-lifecycle`: session metadata/history/checkpointer/runtime state.
- `agentserver-server-push`: agent-originated downstream push events.
- `agentserver-command-mcp`: persisted MCP configuration, discovery, agent reload, and runtime reconciliation.
- `agentserver-sandbox-runtime`: JiuwenBox lifecycle, persisted policy, agent recreation/hot patching, and Landlock status.
- `agentserver-plan-mode-exit`: approval, checkpoint restoration, stale re-entry protection, and client notification.
- `agentserver-schedule-auto-harness`: scheduler startup, durable tasks, autonomous execution identity, and logs.
- `agentserver-history-stream`: persisted history paging, sanitization, streamed events, and frontend reconstruction.
- `session-prewarm-allocation`: Gateway channel sync, pool reconciliation, AgentServer allocation, and first-chat readiness.

Foreground `chat.send`, `chat.resume`, and `chat.user_answer` open a priority window around unary/stream dispatch. The pool globally caps speculative READY/warming work at one slot. A matching warming task is promoted; other speculative tasks are cancelled, and process-global OpenJiuwen registry initialization is serialized before foreground preparation proceeds. Remaining targets stay as lightweight pending keys.

Eligible single-Agent runtime identity follows final `work_mode`: work selects `agent` with no sub-mode, while code selects `code.normal`. Session creation persists that canonical identity and chat selection restores it from locked metadata, so Channel-provided stale mode values cannot bypass a claimed READY child.

## Related Code Symbols

- `_run`: startup lifecycle for the standalone process.
- `AgentWebSocketServer._handle_message`: central request parser and dispatcher.
- `AgentWebSocketServer._handle_stream`: stream response producer with heartbeat and session task tracking.
- `AgentWebSocketServer._handle_unary`: unary response path.
- `AgentWebSocketServer._handle_cancel`: interrupt/cancel path.
- `AgentWebSocketServer.send_push`: server-originated downstream events.

## Verification Evidence

- `tests/unit_tests/test_app_agentserver.py` checks startup/shutdown does not delete agent team directories.
- `tests/unit_tests/agentserver/test_agentserver_modes.py` covers mode resolution, project directory resolution, and stream/mode behavior.
- `tests/unit_tests/agentserver/test_agentserver_acp.py` covers ACP initialization, AgentServer-owned session allocation, explicit-ID rejection, team delete, capabilities, and tool response paths.
- `tests/unit_tests/agentserver/test_agentserver_cli_commands.py` covers slash-command handlers.
- `tests/unit_tests/agentserver/test_agent_ws_connection_close.py` covers disconnect cleanup behavior.
- `tests/unit_tests/agentserver/test_agent_warm_pool.py` covers READY targets, concurrent claims, replenishment, revision replacement, and failure isolation.
- The priority regression cases cover ACP/A2A exclusion, foreground semaphore bypass, chat-time background pause, lazy one-slot dispatch, and post-chat replenishment.
- The focused session-allocation contract run passed 139 tests across AgentServer, Web, project binding, and TUI ownership surfaces on 2026-08-01.
- The priority follow-up passed 139 focused AgentServer/runtime tests; a restarted local stack completed a real ham-snake Web session in about 7.5 seconds from allocated ID to final history record, without the previous 30–40 second outlier.

## Known Gaps

- No full live WebSocket integration evidence found yet for real `websockets.serve`, origin rejection, concurrent inbound frames, ack timing, heartbeat, and disconnect cleanup together.
- `send_push` tracks one current Gateway WebSocket; multiple Gateway connections need explicit ownership rules.
- Broad runtime mutation handlers remain risky even where their `AgentWebSocketServer` entrypoints are now audited; downstream manager, adapter, scheduler, filesystem, and frontend methods still need their own symbol audits.
- The 2026-07-15 scan at `10afedf2` found 0 expired audits among all 128 existing `AgentWebSocketServer` method reviews. The frozen runtime queue still contains 823 methods, including 695 unaudited methods in other server classes; 6 newly observed unaudited methods were not added in this expiration-only update. Current integrity verification trusts 59 method records and flags 69 non-source-expired cards whose entry-document hashes changed.
