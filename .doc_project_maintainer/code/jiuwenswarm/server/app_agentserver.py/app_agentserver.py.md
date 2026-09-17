---
source: jiuwenswarm/server/app_agentserver.py
source_role: runtime_source
audit_scope: default_health_audit
language: python
confidence: confirmed
last_updated: 2026-07-07
---

# `app_agentserver.py`

## Actual Role

Standalone AgentServer process entrypoint. Module import time prepares the workspace, loads env/logging, installs shell safety hooks, and applies an SSE compatibility patch before runtime startup.

## Symbol Inventory

- `_run(host, port)`: async AgentServer service lifecycle.
- `main()`: CLI and environment entrypoint.
- `_PermissionEngineFilter.filter`: conditional logging fallback helper defined only when `config/logging.yaml` is absent; pending inventory review because the AST inventory did not list this local class.

## Key Signals

- Input: CLI args, environment variables, user workspace config and `.env`.
- Output: running WebSocket AgentServer process.
- Main side effects: workspace creation/migration, logging handler setup, env mutation, shell safety patching, extension loading, proactive engine init, teammate bootstrap daemon.
- Main risk: startup work before and after `server.start()` crosses multiple subsystems, so exception cleanup needs careful audit.
- Related tests: `tests/unit_tests/test_app_agentserver.py`, system startup tests under `tests/system_tests`.
