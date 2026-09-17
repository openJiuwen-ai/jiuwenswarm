---
path: tests/unit_tests/agentserver
encoded: tests__unit_tests__agentserver
modules:
  - agentserver-runtime
confidence: confirmed
last_updated: 2026-07-07
read_when: "Checking expected AgentServer behavior or adding tests for runtime handlers."
---

# `tests/unit_tests/agentserver`

## Purpose

Main unit test surface for AgentServer handler contracts, modes, ACP, commands, team runtime, skills, memory, reload behavior, workflow state, and rails.

## Important Test Areas

- `test_agentserver_acp.py`: initialize, session create/switch/delete, team delete, ACP tool responses, capability propagation.
- `test_agentserver_modes.py`: mode normalization, project directory resolution, stream behavior, team-plan and evolution interrupt mapping.
- `test_agentserver_cli_commands.py`: slash-command responses for add-dir, compact, diff, simplify, model, MCP, resume, and session commands.
- `test_agent_ws_connection_close.py`: connection close and cleanup behavior.
- `test_history_payload_limits.py`: bounded history payload behavior.
- `test_agent_reload_scope.py`: scoped reload and pending deep adapter reload.
- `test_team_helpers.py`, `test_remote_member_bootstrap.py`, `test_workflow_state.py`: team, distributed, and workflow behavior.

## Coverage

Strong handler-contract evidence, mostly with fakes and monkeypatches. Full live WebSocket framing, persistent checkpointer, A2X network, and app startup wiring remain less directly covered.
