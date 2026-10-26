# Project Maintainer Index

Status: partial. A 2026-07-15 semantic-hash scan at `10afedf2` found 0 expired audits among the 128 existing `AgentWebSocketServer` method reviews; wider repository coverage remains partial.

## Project

- [Overview](project/overview.md): product and runtime map.
- [Architecture](project/architecture.md): process and boundary sketch.
- [Build Plan](project/build-plan.md): coverage ledger, pending slices, and next work.
- [Open Questions](project/open-questions.md): unresolved evidence gaps.

## Modules

- [AgentServer Runtime](modules/agentserver-runtime/README.md): standalone agent server, WebSocket RPC dispatch, sessions, commands, server push, scheduler, sandbox, ACP.
- [Gateway And Channels](modules/gateway-and-channels/README.md): Gateway clients and channel surfaces that talk to AgentServer.
- [Agent Harness](modules/agent-harness/README.md): agent adapters, rails, team orchestration, memory, skills, and tools.
- [Project Packaging](modules/project-packaging/README.md): package metadata, launch scripts, deploy and installer assets.

## Directories

- [jiuwenswarm/server](directories/jiuwenswarm__server/README.md): AgentServer entrypoint, WebSocket server, runtime services, sandbox, hooks, utilities.
- [jiuwenswarm/gateway/routing](directories/jiuwenswarm__gateway__routing/README.md): AgentServer client and routing support.
- [tests/unit_tests/agentserver](directories/tests__unit_tests__agentserver/README.md): main AgentServer behavior test suite.

## Flows

- [Gateway AgentServer E2A Chat](project/flows/gateway-agentserver-e2a-chat.md)
- [AgentServer Session Lifecycle](project/flows/agentserver-session-lifecycle.md)
- [AgentServer Server Push](project/flows/agentserver-server-push.md)
- [AgentServer MCP Command Lifecycle](project/flows/agentserver-command-mcp.md)
- [AgentServer Sandbox Runtime](project/flows/agentserver-sandbox-runtime.md)
- [AgentServer Plan Mode Exit](project/flows/agentserver-plan-mode-exit.md)
- [AgentServer Scheduled Auto-Harness](project/flows/agentserver-schedule-auto-harness.md)
- [AgentServer History Stream](project/flows/agentserver-history-stream.md)
- [Session Prewarm And Allocation](project/flows/session-prewarm-allocation.md)

## Decisions And Recent Changes

- [ADR-0001](decisions/ADR-0001-agentserver-owned-prewarmed-sessions.md): AgentServer owns prewarmed session identity.
- [CHG-20260731-001](changes/records/CHG-20260731-001-session-prewarm-allocation.md): session allocation and DeepAgent prewarming.
- [CHG-20260801-001](changes/records/CHG-20260801-001-prewarm-foreground-priority.md): foreground chat priority, lazy background warming, and non-blocking runtime probes.
- [CHG-20260801-005](changes/records/CHG-20260801-005-disable-session-prewarm.md): prewarming off by default behind `JIUWENSWARM_AGENT_PREWARM`.
- [CHG-20260803-001](changes/records/CHG-20260803-001-enable-session-prewarm-by-default.md): prewarming on by default; `JIUWENSWARM_AGENT_PREWARM` becomes an opt-out.

## Priority Code Symbols

- app entrypoint: [file](code/jiuwenswarm/server/app_agentserver.py/app_agentserver.py.md), [_run](code/jiuwenswarm/server/app_agentserver.py/_run.md), [main](code/jiuwenswarm/server/app_agentserver.py/main.md)
- websocket server: [file](code/jiuwenswarm/server/agent_ws_server.py/agent_ws_server.py.md), [class](code/jiuwenswarm/server/agent_ws_server.py/Class%20AgentWebSocketServer/Class%20AgentWebSocketServer.md)
- method delivery: [AgentServer method queue](audit-queues/server-method-audit-queue.json) keeps the previously frozen 823-method scope. All 128 `AgentWebSocketServer` methods remain documented and `agent_audited`, with 0 source-expired at `10afedf2`. The fresh scan observed 6 additional unaudited methods outside that frozen queue; they were not added or promoted. Current integrity verification trusts 59 records and flags 69 non-source-expired records with entry-document hash changes. Method cards live under `code/jiuwenswarm/server/agent_ws_server.py/Class AgentWebSocketServer/`.
