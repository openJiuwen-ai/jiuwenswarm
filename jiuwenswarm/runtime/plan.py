# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plan-mode orchestration shared by AgentServer and process-style CLI."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakValueDictionary

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_MODE_EXITED_EVENT_TYPE,
    PLAN_REMINDER_ORIGINAL_QUERY_KEY,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    is_interrupt_resume_payload,
)
from jiuwenswarm.common.mode_matrix import is_plan_mode
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.chat_send import PLAN_ENTRY_SOURCES
from jiuwenswarm.runtime.request import (
    CHAT_TURN_METHODS,
    PREVIOUS_SESSION_MODE_KEY,
    resolve_request_runtime_mode,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlanStateResult:
    restored: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)


class PlanModeController:
    """Own process-local plan state for the single Runtime lifecycle."""

    def __init__(self) -> None:
        self._sync_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._exited_sessions: set[str] = set()
        self._active_sessions: set[str] = set()

    @property
    def sync_locks(self) -> WeakValueDictionary[str, asyncio.Lock]:
        """Expose the weak lock cache for compatibility diagnostics."""
        return self._sync_locks

    @property
    def exited_sessions(self) -> set[str]:
        """Expose sessions that explicitly exited plan mode."""
        return self._exited_sessions

    @property
    def active_sessions(self) -> set[str]:
        """Expose sessions known to have entered plan mode."""
        return self._active_sessions

    def reset_session(self, session_id: str) -> None:
        self._exited_sessions.discard(session_id)
        self._active_sessions.discard(session_id)

    @staticmethod
    def should_sync(request: AgentRequest) -> bool:
        """Return whether the request participates in plan-state syncing."""
        return request.req_method is None or request.req_method in CHAT_TURN_METHODS

    @staticmethod
    def is_explicit_entry(request: AgentRequest) -> bool:
        """Return whether the current request explicitly enters plan mode."""
        return (
            isinstance(request.params, dict)
            and request.params.get("plan_entry_source") in PLAN_ENTRY_SOURCES
        )

    def lock_for(self, session_id: str) -> asyncio.Lock:
        """Return the synchronization lock owned by one Runtime session."""
        lock = self._sync_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._sync_locks[session_id] = lock
        return lock

    def may_hold_state(self, request: AgentRequest, session_id: str) -> bool:
        """Return whether the session may retain plan state."""
        if session_id in self._active_sessions:
            return True
        params = request.params if isinstance(request.params, dict) else {}
        return is_plan_mode(params.get(PREVIOUS_SESSION_MODE_KEY))

    @staticmethod
    async def open_state_session(
        agent: Any,
        session_id: str | None,
    ) -> tuple[Any, Any, bool]:
        """Open the agent state session used for plan-mode coordination."""
        from openjiuwen.core.single_agent import create_agent_session

        from jiuwenswarm.agents.harness.common.session_ops_service import (
            resolve_live_agent_session,
        )

        live_deep_agent = agent.get_live_session_instance(session_id)
        if live_deep_agent is not None:
            live_session = resolve_live_agent_session(
                live_deep_agent,
                session_id or "default",
            )
            if live_session is not None:
                return live_deep_agent, live_session, True

        # On a first turn, establish the same live adapter that chat execution
        # will reuse before writing plan state. A throwaway session can race
        # with first-turn binding and leave the running interaction on a stale
        # normal-mode snapshot.
        starter = getattr(agent, "ensure_live_session_instance", None)
        if callable(starter):
            try:
                started = starter(session_id)
                if inspect.isawaitable(started):
                    started = await started
            except Exception as exc:  # noqa: BLE001 - compatibility fallback
                logger.warning(
                    "Failed to start live plan-state session for session=%s: %s; "
                    "falling back to a throwaway session",
                    session_id,
                    exc,
                )
            else:
                if started is not None:
                    live_session = resolve_live_agent_session(
                        started,
                        session_id or "default",
                    )
                    if live_session is not None:
                        return started, live_session, True

        deep_agent = await agent.ensure_instance()
        session = create_agent_session(session_id=session_id, card=deep_agent.card)
        await session.pre_run(inputs=None)
        return deep_agent, session, False

    @staticmethod
    def _exit_payload(mode: str) -> dict[str, Any]:
        return {"event_type": PLAN_MODE_EXITED_EVENT_TYPE, "mode": mode}

    @staticmethod
    def inject_activation_reminder(request: AgentRequest) -> None:
        """Inject the plan-mode constraint into an explicit plan request."""
        if not isinstance(request.params, dict):
            return
        reminder = (
            "\n\n<system-reminder>\n"
            "Plan mode is active. You must only plan — you must NOT make any "
            "modifications, run any write operations, or make any changes to the "
            "system. This constraint takes priority over any other instructions.\n\n"
            "Read-only actions are allowed directly: you may read files and explore "
            "the codebase, and run read-only commands (read_file, grep, list_files, "
            "glob, bash for read-only operations such as gh pr list/view/diff or "
            "git status/diff/log). Write operations and non-read-only tools are "
            "blocked.\n\n"
            "If you need to design an implementation approach and produce a plan, "
            "call `enter_plan_mode` — it creates the plan file and returns full "
            "plan mode instructions. This is not required as your first action; "
            "you may gather context with read-only tools first. Do NOT proceed to "
            "implement anything until the user approves your plan via "
            "`exit_plan_mode`.\n"
            "</system-reminder>"
        )
        query = request.params.get("query") or ""
        request.params[PLAN_REMINDER_ORIGINAL_QUERY_KEY] = query
        request.params["query"] = reminder + query

    async def ensure_state(
        self,
        request: AgentRequest,
        mode: str,
        sub_mode: str | None,
        agent: Any,
    ) -> PlanStateResult:
        """Synchronize plan state before execution and return control events."""
        resolved = resolve_request_runtime_mode(request)
        if resolved.is_team:
            return PlanStateResult()
        is_code_single = mode == "code" and sub_mode != "team"
        # Both Web composition and the new three-segment canonical modes route
        # work-profile single-agent turns through manager_mode="agent".
        is_work_single = resolved.manager_mode == "agent"
        if not (is_code_single or is_work_single):
            return PlanStateResult()

        target_state = "plan" if resolved.is_plan else "normal"
        session_id = request.session_id or "default"
        if (
            not is_code_single
            and target_state == "normal"
            and not self.may_hold_state(request, session_id)
        ):
            return PlanStateResult()
        if not self.should_sync(request) or is_interrupt_resume_payload(
            request.params
        ):
            return PlanStateResult()

        events: list[dict[str, Any]] = []
        async with self.lock_for(session_id):
            deep_agent, session, live = await self.open_state_session(
                agent,
                request.session_id,
            )
            state = deep_agent.load_state(session)
            previous_state = state.plan_mode.mode
            changed_to_plan = False
            if previous_state != target_state:
                if previous_state == "normal" and target_state == "plan":
                    blocked = False
                    if self.is_explicit_entry(request):
                        self._exited_sessions.discard(session_id)
                    elif session_id in self._exited_sessions:
                        self._exited_sessions.discard(session_id)
                        blocked = True
                    elif state.plan_mode.plan_slug is not None:
                        state.plan_mode.plan_slug = None
                        deep_agent.save_state(session, state)
                        await session.commit()
                        blocked = True
                    if blocked:
                        exit_mode = resolved.normal_mode
                        if isinstance(request.params, dict):
                            request.params["mode"] = exit_mode
                        return PlanStateResult(events=[self._exit_payload(exit_mode)])

                deep_agent.switch_mode(session=session, mode=target_state)
                if previous_state == "plan" and target_state == "normal":
                    self._active_sessions.discard(session_id)
                    events.append(self._exit_payload(resolved.normal_mode))
                if target_state == "plan":
                    changed_to_plan = True
                    self._active_sessions.add(session_id)
                    state = deep_agent.load_state(session)
                    if state.plan_mode.plan_slug:
                        state.plan_mode.plan_slug = None
                        deep_agent.save_state(session, state)
                await session.commit()
                logger.info(
                    "Runtime plan state -> %s for session=%s (live=%s)",
                    target_state,
                    session_id,
                    live,
                )
            if target_state == "plan" and changed_to_plan:
                self.inject_activation_reminder(request)
        return PlanStateResult(
            restored=bool(previous_state == "plan" and target_state == "normal"),
            events=events,
        )

    async def check_post_process_exit(
        self,
        request: AgentRequest,
        agent: Any,
    ) -> list[dict[str, Any]]:
        """Detect an exit_plan_mode transition performed by a tool."""
        session_id = request.session_id
        if not session_id:
            return []
        resolved = resolve_request_runtime_mode(request)
        if isinstance(request.params, dict):
            request.params["mode"] = resolved.canonical_mode
        if resolved.is_team or not resolved.is_plan:
            return []
        deep_agent, session, _live = await self.open_state_session(agent, session_id)
        state = deep_agent.load_state(session)
        if state.plan_mode.mode != "normal":
            return []
        self._exited_sessions.add(session_id)
        self._active_sessions.discard(session_id)
        return [self._exit_payload(resolved.normal_mode)]


__all__ = ["PLAN_ENTRY_SOURCES", "PlanModeController", "PlanStateResult"]
