---
id: CHG-20260803-001
date: 2026-08-03
title: Switch background session prewarming back on by default
confidence: confirmed
modules:
  - agentserver-runtime
flows:
  - session-prewarm-allocation
decisions:
  - ADR-0001
---

# Switch Background Session Prewarming Back On By Default

## Change

`JIUWENSWARM_AGENT_PREWARM` turns from an opt-in into an opt-out. `AgentWarmPool` now enables itself unless the variable is set to `0`/`false`/`no`/`off`; `1`/`true`/`yes`/`on` keeps working, and any other non-empty value is logged as unrecognized and treated as enabled so a typo cannot silently disable warming.

Nothing else changed. The dormant behavior introduced by [CHG-20260801-005](CHG-20260801-005-disable-session-prewarm.md) still applies verbatim while the switch is off: `sync` returns zero statistics and `claim` returns a freshly allocated ID with `prewarm_status="bypassed"`. The explicit `enabled` constructor argument still overrides the environment.

## Compatibility

No wire or call-site change. Deployments that relied on the default being off must now set `JIUWENSWARM_AGENT_PREWARM=0`. Deployments that had set `JIUWENSWARM_AGENT_PREWARM=1` are unaffected.

## Verification

- `tests/unit_tests/agentserver/test_agent_warm_pool.py`: added `test_prewarm_is_enabled_unless_the_environment_opts_out` covering unset/off/on, and switched the disabled-pool case to opt out explicitly. 12 passed, with the one pre-existing macOS-only failure in `test_warm_key_normalizes_project_directory`, where the test lowercases a path that `os.path.normcase` leaves untouched on POSIX.
