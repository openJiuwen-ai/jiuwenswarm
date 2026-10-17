---
id: CHG-20260909-001
title: Make Goal executions native Session Runtime work
type: refactor
date: 2026-09-09
modules:
  - runtime-session
  - agentserver-runtime
flows:
  - runtime-session-reference-chain
confidence: confirmed
---

# Make Goal Executions Native Session Runtime Work

## What Changed

`AgentRuntime` now classifies Goal set/resume, Goal get/pause/clear, Goal attach, follow-up, steer, and interaction answers as explicit Session work kinds. `RuntimeSessionCoordinator` registers and owns their execution lifecycle directly. Ordinary chat alone uses the Session scheduler; Goal and control work stays outside that lane so the Goal SDK remains the single owner of its `control_lock`, scheduler, and output lease.

Interaction suspension now records the event's concrete request or interaction ID. Control delivery matches that ID exactly rather than selecting the newest active execution, allowing chat and Goal interactions to coexist without misrouting an answer. Session cancellation, close, and shutdown cancel both scheduled chat and directly registered Goal/control executions with the same bounded-wait contract.

No Goal compatibility mode, latest-execution fallback, duplicate Goal lock, or transport-owned execution state was added. AgentServer, Web, TUI, ACP, and IM keep their protocol behavior and enter the new ownership through the existing `AgentRuntime` boundary.

## Verification

Runtime, AgentServer, and Gateway selections passed 466 deterministic tests; Goal history/evolution selections passed 57. A configured-provider Goal run emitted an actual `ask_user`, ended its initial stream, accepted the answer through the matching Runtime execution, and completed with the expected terminal response. The repository's standalone Goal adapter test cannot collect on this machine because it prepends an unrelated older `agent-core` checkout that does not contain `openjiuwen.harness`; the installed project SDK does contain that package and powered the passing live test.
