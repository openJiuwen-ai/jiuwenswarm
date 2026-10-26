---
id: CHG-20260731-001
date: 2026-07-31
title: Add AgentServer-owned session allocation and DeepAgent prewarming
confidence: confirmed
modules:
  - agentserver-runtime
  - gateway-and-channels
  - agent-harness
flows:
  - session-prewarm-allocation
decisions:
  - ADR-0001
---

# Add AgentServer-Owned Session Allocation And DeepAgent Prewarming

## Change

Added a process-local `AgentWarmPool`, `agent.prewarm.sync`, AgentServer ID allocation with `create_token` idempotency, DeepAdapter `prepare_session`, asynchronous configuration reconciliation, and first-chat waiting on the shared initialization task.

Web, TUI, controlled IM channels, ACP, A2A, SSH, fork, and single-Agent Cron now consume AgentServer-returned IDs. Team/Swarm modes bypass prewarming. Prewarm markers are hidden from normal session metadata/listing and isolated by boot ID.

## Compatibility

`session.create` no longer accepts a caller-provided new ID. Existing sessions must use `session.switch`. `/new [id]` is removed; TUI `--session` restores only.

## Verification

- Warm-pool and Cron focused unit tests: 68 passed.
- Web creation tests: 5 passed.
- Web production build: passed.
- TUI TypeScript typecheck: passed.
- Cron scheduler suite updated for the two-step create/send contract.
- Session allocation contract suites for AgentServer ACP/plan mode, Web identity, project binding, and TUI forwarding/KVC ownership: 139 passed.
- Full Python unit suite on Windows: 3,791 passed, 4 skipped, and 14 unrelated failures. The remaining failures depend on POSIX shell/path assumptions, including one cleanup test that hand-builds a non-normalized POSIX cache key, or on occupied local ports.
- Web creation preserves authenticated connection ownership by replacing any request-body `user_id` before forwarding `session.create`.
- CodeCheck follow-up removed unreachable pre-AgentServer Web/TUI creation code, extracted warm-pool safety/revision predicates, and added a public DeepAdapter stable-config boundary. The expanded focused suite passed 166 tests.
