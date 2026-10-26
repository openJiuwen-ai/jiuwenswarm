---
id: agentserver-plan-mode-exit
name: AgentServer Plan Mode Exit
status: partial
confidence: confirmed
last_updated: 2026-07-14
user_visible_surface: "TUI plan approval dialog, plan-mode indicator, and the return to code.normal after approval."
source_of_truth:
  - "session checkpointer: DeepAgentState.plan_mode"
  - "workspace plan file resolved from plan_mode.plan_slug"
modules:
  - agentserver-runtime
  - agent-harness
  - gateway-and-channels
directories:
  - jiuwenswarm/server
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/agents/harness/code/rails
  - jiuwenswarm/agents/harness/common/rails/interrupt
  - jiuwenswarm/channels/tui/frontend/src
  - tests/unit_tests/agentserver
code_symbols:
  - AgentWebSocketServer._ensure_code_mode_state
  - AgentWebSocketServer._check_post_process_plan_exit
  - AgentWebSocketServer._push_plan_mode_exited
  - PlanApprovalInterruptRail.resolve_interrupt
  - CodeAgentModeRail.after_tool_call
  - convert_interactions_to_ask_user_question
entrypoints:
  - "TUI /plan [request] -> chat.send(mode=code.plan)"
  - "LLM exit_plan_mode tool call -> approval interrupt"
  - "TUI approval answer -> chat.send interrupt resume"
---

# AgentServer Plan Mode Exit

## Outcome

A code-mode user can inspect a generated plan, approve or reject it, and—only after approval—resume in `code.normal`. Durable agent state, stale-mode guards, and `plan.mode_exited` keep the agent and TUI from re-entering the completed plan.

## Causal Path

1. `/plan` sets TUI mode to `code.plan` and attaches one-shot `plan_entry_source: slash_command` to `chat.send`. `_prepare_code_mode_chat_turn` selects the project-scoped code agent; `_ensure_code_mode_state` hydrates the session, switches durable mode to `plan`, clears stale slug state, and adds the activation reminder.
2. The model calls `enter_plan_mode`, which assigns `plan_slug`; plan-mode rails restrict writes to the resolved plan file. After writing it, the model calls `exit_plan_mode`.
3. `PlanApprovalInterruptRail` (priority 78) intercepts that tool before execution. It reads the file and emits a confirm interrupt with path/slug and a 3,000-character preview. Normalization converts it to `chat.ask_user_question` with `request_id`, approve/reject options, and plan metadata.
4. TUI stores `pendingQuestion` and current `resumeMode`. Its answer is a new empty-query `chat.send` carrying the same `request_id`, answers, source, resume mode, and plan metadata; rejection may add revision feedback.
5. `is_interrupt_resume_payload` recognizes this request, so `_ensure_code_mode_state` skips synchronization: stale client `code.plan` cannot overwrite a just-restored checkpoint. The adapter supplies `InteractiveInput` to the suspended confirm rail. Approval executes the tool; rejection yields `RejectResult`, sets `_plan_rejected`, skips it, and stays in plan mode.
6. Approved `ExitPlanModeTool.invoke` reads the full plan and immediately calls `restore_mode_after_plan_exit`: mode becomes `pre_plan_mode` or `normal`, `pre_plan_mode` is cleared, `plan_slug` is retained, and `save_state` updates checkpoint-backed session state. For an empty plan, `CodeAgentModeRail.after_tool_call` restores once a real result exists. The exit notification tells the model writes are allowed.
7. Unary and streaming `finally` blocks run `_check_post_process_plan_exit`, which creates a fresh session view, runs `pre_run`, and reloads persisted state. If a request that began as `code.plan` is now normal, it records `_plan_exited_sessions` and pushes `plan.mode_exited` with channel/session identity. TUI changes a current `code.*` mode to `code.normal`.

## State Classification

- Durable source of truth: checkpointer-backed `DeepAgentState.plan_mode` and the workspace plan file. Request params and TUI mode are projections.
- Process-local coordination: `_session_mode_sync_locks` serializes request-start mode synchronization per session. `_plan_exited_sessions` is a one-shot stale-reentry guard, not durable truth.
- Client/transient state: TUI `mode`, pending entry/question/resume state; interrupt correlation; the exit push.
- Derived output: the approval preview and plan metadata, the model-facing exit notification, and the mode-exited event.

## Replay, Restore, Or Reconstruction

`pre_run` reconstructs state from the checkpointer. A missed exit push does not roll back the durable exit. On the next ordinary `code.plan` chat, `_ensure_code_mode_state` rewrites to `code.normal` when either the session is in `_plan_exited_sessions` or normal persisted state still has a `plan_slug`; the latter is cleared and persisted. Both paths repush the event.

Explicit `/plan` is the exception: its source marker discards the flag and permits fresh entry, which clears the old slug. Interrupt resumes skip repair to continue the suspended tool. Background methods do not synchronize mode.

## Contract

- Plan request: `chat.send` carries `mode: code.plan`; explicit entry additionally carries one-shot `plan_entry_source: slash_command`.
- Approval request: `chat.ask_user_question` carries non-empty `request_id`, `source: confirm_interrupt`, options, and plan metadata.
- Approval response: `chat.send` carries the same ID, non-empty answers, source, empty query, and captured resume mode.
- Exit notification: server push payload is `{event_type: "plan.mode_exited", mode: "code.normal"}` and is routed with channel/session identity. It has no checkpoint revision or approval request ID.

## Consumer State And Output

The TUI renders the interrupt preview; the executing model receives the full plan from `ExitPlanModeTool`. Approve continues the run in normal mode. Reject keeps `code.plan`, returns feedback, and emits no exit event. TUI mode is not the commit point.

## Failure, Ordering, And Identity

- The per-session lock covers only `_ensure_code_mode_state`; restore, post-process reload, push, and answer correlation run outside it. Concurrent same-session requests can observe different exit-boundary snapshots.
- The lock map is unpruned. `_plan_exited_sessions` is process-local and lost on restart; persisted slug is the durable fallback.
- Post-process checking runs from `finally` and reads state, not tool output. Replayed `code.plan` traffic already in normal mode may duplicate an exit event and guard entry. The event lacks an idempotency key, though TUI assignment is idempotent.
- A push can be dropped when no Gateway socket is active. Recovery occurs only when a later chat hits a stale-mode guard; no reconnect-time mode hydration was found.
- Plan-file read failure yields an empty approval body. Empty-plan approval is restored by the after-tool fallback. Restore failure in that fallback is logged and swallowed, leaving persisted plan mode unchanged and suppressing the server's exit detection.
- `request_id` correlates approval. Suspended-tool matching and duplicate/stale answers are delegated to the harness and lack end-to-end replay coverage.

## Verification

Evidence was checked in AgentServer, rails, interrupt helpers, pinned `openjiuwen==0.1.15.post3`, and TUI state/handlers. Tests cover mode resolution/sync, slash re-entry, sync exclusions, interrupt routing, approval conversion/mounting, plan write restrictions, and TUI approval/reject UI.

Focused checks are `test_plan_mode_orchestration.py`, `test_code_agent_mode_rail.py`, `test_agentserver_modes.py`, `test_structured_ask_user.py`, `test_swarm_assembly.py`, and TUI `run-tests.mjs`.

## Known Gaps

No focused test covers `_check_post_process_plan_exit`, exit-push delivery into TUI state, missed/duplicate push, `plan_slug` restart fallback, restore failure, stale or duplicate approval answers, or concurrent same-session plan exit versus a new chat. A live Gateway-WebSocket-TUI test is still needed to confirm ordering across approval, checkpoint persistence, resumed model output, and the final mode indicator.
