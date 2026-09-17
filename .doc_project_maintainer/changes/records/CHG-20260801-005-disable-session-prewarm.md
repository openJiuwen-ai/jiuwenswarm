---
id: CHG-20260801-005
date: 2026-08-01
title: Switch background session prewarming off by default
confidence: confirmed
modules:
  - agentserver-runtime
flows:
  - session-prewarm-allocation
decisions:
  - ADR-0001
---

# Switch Background Session Prewarming Off By Default

## Change

`AgentWarmPool` gained an `enabled` switch that defaults to the `JIUWENSWARM_AGENT_PREWARM` environment variable and is off unless it is set to `1`/`true`/`yes`/`on`.

While off, `sync` (and therefore `refresh`) returns zero statistics without advancing the revision, scheduling preparation, or writing markers, and `claim` returns a freshly allocated ID with `prewarm_hit=false` and `prewarm_status="bypassed"` — the path Team/Swarm creation already used. No agent is created and no `prepare_session` runs before the first request; sessions initialize lazily on `chat.send` as they did before CHG-20260731-001.

AgentServer-owned ID allocation, `create_token` idempotency, `session.create` validation, and the Gateway `agent.prewarm.sync` RPC are unchanged; the RPC simply becomes a zero-stat no-op.

## Compatibility

No wire or call-site change. `prewarm_status` is always `bypassed` and `prewarm_hit` always `false` while the switch is off. Setting `JIUWENSWARM_AGENT_PREWARM=1` restores the CHG-20260731-001 behavior.

## Verification

- `tests/unit_tests/agentserver/test_agent_warm_pool.py`: added a default-off case asserting zero stats, a bypassed claim, and no slots/tasks/preparation; the existing pool cases opt in explicitly. One pre-existing macOS-only failure remains in `test_warm_key_normalizes_project_directory`, where the test lowercases a path that `os.path.normcase` leaves untouched on POSIX.
- Session allocation contract suites for AgentServer ACP/plan mode, Web identity, project binding, TUI forwarding/KVC ownership, and the Cron scheduler: 201 passed.
