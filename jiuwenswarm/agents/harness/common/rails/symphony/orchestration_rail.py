# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt and candidate lifecycle rail for Symphony orchestration."""

from __future__ import annotations

import json
import re
import threading
from copy import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.ability_manager import AbilityExecutionError
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
)
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.symphony.llm import (
    SYMPHONY_LLM_CONFIG_REF_KEY,
    bind_request_llm_config,
    reset_request_llm_config,
)

_ConfigBaseProvider = dict[str, Any] | Callable[[], dict[str, Any] | None] | None
_SHORTLIST_EXTRA_KEY = "symphony_candidate_skill_ids"
_GRAPH_BUILD_TIMEOUT_EXTRA_KEY = "symphony_graph_build_timeout"
_REQUEST_LLM_TOKEN_ATTR = "_symphony_request_llm_config_token"
_GRAPH_TOOL_NAMES = frozenset(
    {
        "symphony_compose_graph",
        "symphony_refresh_graph",
    }
)
_ABILITY_MANAGER_TIMEOUT_PATTERN = re.compile(
    r"Tool '([^']+)' timed out after ([0-9]+(?:\.[0-9]+)?)s"
)

_MANUAL_GRAPH_BUILD_CONTENT = {
    "cn": (
        "技能较多时，技能图谱构建时间可能较长，本次会话内构建已超时。请前往「我的技能」>「技能图谱」，"
        "点击「增量构建」并等待构建完成；完成后请重新发送任务。为避免再次超时，本轮不会重复调用图谱构建。"
    ),
    "en": (
        "With many skills, building the Skill Graph can take a while and timed out in this session. "
        "Go to My Skills > Skill Graph, click Incremental Build, and wait for it to finish; then resend your task. "
        "To avoid another timeout, this round will not call the graph-building tools again."
    ),
}

_GRAPH_PREPARING_CONTENT = {
    "cn": (
        "技能图谱正在构建，请等待页面构建完成后重新发送任务。本轮不会重复调用图谱工具。"
    ),
    "en": (
        "The Skill Graph is currently being built. Wait for the build to finish, "
        "then resend your task. This round will not call the graph tools again."
    ),
}

_SENSITIVE_INPUT_NAME = re.compile(
    r"(?:password|passwd|secret|token|api[_ -]?key|credential|authorization|"
    r"密码|口令|密钥|令牌|凭据)",
    re.IGNORECASE,
)
_MAX_RESUME_ANSWER_CHARS = 1024
_MAX_RESUME_ANSWERS = 16
_INVOKE_ROUTE_KEY = "_symphony_orchestration_route"


@dataclass(frozen=True)
class _PausedInvokeKey:
    """Exact identity for a paused compose; no query or clock fallbacks."""

    session_id: str
    capture_mode: str
    owner_id: str
    component_id: str


@dataclass(frozen=True)
class _InvokeScope:
    """Route shared by the outer DeepAgent and its inner ReActAgent."""

    session_id: str
    capture_mode: str
    owner_id: str


@dataclass
class _SymphonyInvokeState:
    """Only the minimum non-secret state needed for a HITL recompose."""

    session_id: str
    capture_mode: str
    owner_id: str
    original_query: str
    candidate_skill_ids: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    missing_inputs: Any = None
    awaiting_input: bool = False
    pending_recompose: bool = False
    answered: bool = False
    generation: int = 0
    valid: bool = True
    route_token: str = ""


class SymphonyOrchestrationRail(DeepAgentRail):
    """Manage Symphony guidance, candidate injection, and viewed Skills."""

    priority = 98
    SECTION_NAME = "symphony_orchestration"
    SECTION_PRIORITY = 42
    COMPOSE_TOOL_NAME = "symphony_compose_graph"

    def __init__(self, *, config_base: _ConfigBaseProvider = None) -> None:
        super().__init__()
        self._config_base = config_base
        self.system_prompt_builder = None
        self._paused_states: dict[_PausedInvokeKey, _SymphonyInvokeState] = {}
        self._active_states: dict[_InvokeScope, _SymphonyInvokeState] = {}
        self._active_route_states: dict[str, _SymphonyInvokeState] = {}
        self._outer_states: dict[int, _SymphonyInvokeState] = {}
        self._scope_generations: dict[_InvokeScope, int] = {}
        self._quarantined_scopes: dict[_InvokeScope, set[int]] = {}
        self._paused_states_lock = threading.RLock()

    def init(self, agent: Any) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        _ = agent
        try:
            if self.system_prompt_builder is not None:
                self.system_prompt_builder.remove_section(self.SECTION_NAME)
        finally:
            # A rail can outlive an adapter reconfiguration.  Never attach an
            # answer from that later adapter to an old paused invocation.
            with self._paused_states_lock:
                for state in self._active_states.values():
                    state.valid = False
                for state in self._paused_states.values():
                    state.valid = False
                for state in self._outer_states.values():
                    state.valid = False
                self._paused_states.clear()
                self._active_states.clear()
                self._active_route_states.clear()
                self._outer_states.clear()
                self._quarantined_scopes.clear()
                self._scope_generations.clear()
            self.system_prompt_builder = None

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """Claim one explicit InteractiveInput response, or start fresh."""

        scope = self._scope_for_ctx(ctx)
        if scope is None:
            return
        inputs = getattr(ctx, "inputs", None)
        query = getattr(inputs, "query", None)
        route_token = self._bind_outer_route(ctx)
        if route_token is None:
            return
        resumed = self._claim_paused_state(scope, query, id(ctx), route_token)
        if resumed is not None:
            resumed.awaiting_input = False
            resumed.pending_recompose = True
            self._remember_resume_answers(resumed, query)
            return

        # A normal new turn, an invalid response, or an ambiguous response
        # must never inherit an old compose plan from this scope.
        with self._paused_states_lock:
            if self._begin_conflict_quarantine_locked(scope, id(ctx)):
                state = _SymphonyInvokeState(
                    session_id=scope.session_id,
                    capture_mode=scope.capture_mode,
                    owner_id=scope.owner_id,
                    original_query=query if isinstance(query, str) else "",
                    valid=False,
                    route_token=route_token,
                )
                self._outer_states[id(ctx)] = state
                return
            generation = self._invalidate_scope_locked(scope)
            state = _SymphonyInvokeState(
                session_id=scope.session_id,
                capture_mode=scope.capture_mode,
                owner_id=scope.owner_id,
                original_query=query if isinstance(query, str) else "",
                generation=generation,
                route_token=route_token,
            )
            self._active_states[scope] = state
            self._active_route_states[route_token] = state
            self._outer_states[id(ctx)] = state

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        """Keep a needs-input state only for a valid exact interruption."""

        outer_context_id = id(ctx)
        with self._paused_states_lock:
            state = self._outer_states.pop(outer_context_id, None)
        if state is None:
            return
        scope = _InvokeScope(state.session_id, state.capture_mode, state.owner_id)
        try:
            result = getattr(getattr(ctx, "inputs", None), "result", None)
            if self._is_cancelled_or_error_result(result):
                self._invalidate_state_scope(state)
                return
            if not state.awaiting_input:
                self._remove_active_state(state)
                return
            component_ids = self._interrupt_component_ids(result)
            if not component_ids:
                self._invalidate_state_scope(state)
                return
            self._pause_state(state, component_ids)
        finally:
            with self._paused_states_lock:
                self._finish_quarantine_locked(scope, outer_context_id)
                self._cleanup_scope_generation_locked(scope)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        self._remove_graph_tools_after_timeout(ctx)
        self._sync_orchestration_guidance(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._gate_or_rewrite_resumed_tool_call(ctx)
        self._bind_request_model(ctx)
        self._inject_shortlisted_candidates(ctx)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        try:
            self._remember_needs_input_or_ready_plan(ctx)
            self._terminate_structured_graph_preparing(ctx)
            self._terminate_structured_graph_build_timeout(ctx)
            self._remember_successful_skill_view(ctx)
        finally:
            self._serialize_tool_call_arguments(ctx)
            self._reset_request_model(ctx)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        """Turn the outer AbilityManager timeout into the same terminal result."""
        try:
            if not self._is_graph_tool_call(ctx) or not self._is_outer_graph_timeout(
                ctx
            ):
                return
            self._terminate_graph_build_timeout(ctx, self._outer_timeout_payload(ctx))
        finally:
            self._serialize_tool_call_arguments(ctx)
            self._reset_request_model(ctx)

    def _gate_or_rewrite_resumed_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Do not let a resumed loop run Skills on a stale composition."""

        if not isinstance(getattr(ctx, "inputs", None), ToolCallInputs):
            return
        state = self._active_state_for_ctx(ctx)
        if state is None:
            return
        tool_name = self._tool_name(ctx.inputs)
        if self._is_skill_tool(tool_name) and (
            state.awaiting_input or state.pending_recompose
        ):
            self._reject_skill_until_composed(ctx)
            return
        if tool_name != self.COMPOSE_TOOL_NAME:
            return
        if state.awaiting_input:
            # This is still the original loop.  It has no user response and
            # must not spin on needs_input.
            self._reject_compose_until_input(ctx)
            return
        if not state.pending_recompose:
            return

        args = self._tool_args(ctx.inputs)
        # The plan is deliberately tied to the exact candidates chosen before
        # the question.  Do not let the model widen or drift that shortlist.
        args["query"] = self._resume_query(state)
        args["candidate_skill_ids"] = list(state.candidate_skill_ids)
        ctx.inputs.tool_args = args
        if ctx.inputs.tool_call is not None:
            ctx.inputs.tool_call.arguments = args

    def _remember_needs_input_or_ready_plan(self, ctx: AgentCallbackContext) -> None:
        if not self._is_compose_tool_call(ctx):
            return
        state = self._active_state_for_ctx(ctx)
        if state is None or not isinstance(ctx.inputs, ToolCallInputs):
            return
        result = ctx.inputs.tool_result
        status, metadata = self._planned_graph_status(result)
        if status == "needs_input":
            state.candidate_skill_ids = self._candidate_skill_ids(ctx.inputs)
            state.missing_inputs = self._safe_json_value(
                metadata.get("missing_inputs") if metadata is not None else None
            )
            state.awaiting_input = True
            state.pending_recompose = False
            return
        if state.pending_recompose and status == "ready":
            # A resumed invocation may execute Skills only after a complete,
            # explicitly ready replacement plan. Invalid/no-plan/failure
            # results retain the gate; graph timeout/preparing keep their
            # existing terminal lifecycle below.
            state.pending_recompose = False
            state.awaiting_input = False

    def _reject_skill_until_composed(self, ctx: AgentCallbackContext) -> None:
        assert isinstance(ctx.inputs, ToolCallInputs)
        payload = {
            "success": False,
            "reason": "symphony_plan_not_ready",
            "retryable": True,
            "required_tool": self.COMPOSE_TOOL_NAME,
            "detail": "Call symphony_compose_graph before executing skill_tool.",
        }
        self._skip_tool(ctx, payload)

    def _reject_compose_until_input(self, ctx: AgentCallbackContext) -> None:
        assert isinstance(ctx.inputs, ToolCallInputs)
        payload = {
            "success": False,
            "reason": "symphony_input_required",
            "retryable": False,
            "detail": "Wait for the user's response before composing again.",
        }
        self._skip_tool(ctx, payload)

    @staticmethod
    def _skip_tool(ctx: AgentCallbackContext, payload: dict[str, Any]) -> None:
        assert isinstance(ctx.inputs, ToolCallInputs)
        ctx.inputs.tool_result = payload
        tool_call = ctx.inputs.tool_call
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if not tool_call_id and tool_call is not None:
            # AbilityManager consumes the marker by ID.  A provider may omit
            # one, but leaving it blank would make this safety rejection a
            # no-op.  Bind a fresh per-call ID instead of using the legacy
            # invocation-wide marker shared by parallel calls.
            tool_call_id = f"symphony-skip-{uuid4().hex}"
            tool_call.id = tool_call_id
        ctx.inputs.tool_msg = ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=tool_call_id,
        )
        if tool_call_id:
            ctx.extra.setdefault("_skip_tool_calls", {})[tool_call_id] = True

    @staticmethod
    def _serialize_tool_call_arguments(ctx: AgentCallbackContext) -> None:
        """Restore the model-message contract after Core executes dict args."""

        inputs = getattr(ctx, "inputs", None)
        tool_call = getattr(inputs, "tool_call", None)
        arguments = getattr(tool_call, "arguments", None)
        if tool_call is not None and isinstance(arguments, Mapping):
            tool_call.arguments = json.dumps(arguments, ensure_ascii=False)

    def _scope_for_ctx(self, ctx: AgentCallbackContext) -> _InvokeScope | None:
        """Get an explicit session/owner scope without query-based guessing."""

        inputs = getattr(ctx, "inputs", None)
        session = getattr(ctx, "session", None)
        get_session_id = getattr(session, "get_session_id", None)
        session_id = (
            str(get_session_id() or "").strip() if callable(get_session_id) else ""
        )
        if not session_id:
            session_id = str(getattr(inputs, "conversation_id", "") or "").strip()
        if not session_id:
            return None

        runtime_extra = self._runtime_extra(ctx)
        agent = getattr(ctx, "agent", None)
        team_id = self._scope_value(ctx, runtime_extra, "team_id")
        member_id = self._scope_value(ctx, runtime_extra, "member_id")
        team_id = team_id or str(getattr(agent, "team_id", "") or "").strip()
        member_id = member_id or str(getattr(agent, "member_id", "") or "").strip()
        capture_mode = (
            self._scope_value(ctx, runtime_extra, "capture_mode")
            or str(getattr(agent, "capture_mode", "") or "")
        ).lower()
        if capture_mode not in {"agent", "team"}:
            role = str(
                runtime_extra.get("role") or getattr(agent, "role", "") or ""
            ).lower()
            capture_mode = "team" if team_id or role == "leader" else "agent"
        owner_id = team_id if capture_mode == "team" else member_id
        if not owner_id:
            card = getattr(agent, "card", None)
            owner_id = str(
                getattr(card, "id", None)
                or getattr(agent, "id", None)
                or getattr(agent, "name", None)
                or "default"
            ).strip()
        return _InvokeScope(session_id, capture_mode, owner_id)

    @staticmethod
    def _runtime_extra(ctx: AgentCallbackContext) -> Mapping[str, Any]:
        inputs = getattr(ctx, "inputs", None)
        run_context = getattr(inputs, "run_context", None) or ctx.extra.get(
            "run_context"
        )
        extra = (
            run_context.get("extra")
            if isinstance(run_context, Mapping)
            else getattr(run_context, "extra", None)
        )
        return extra if isinstance(extra, Mapping) else {}

    @staticmethod
    def _scope_value(
        ctx: AgentCallbackContext, runtime_extra: Mapping[str, Any], name: str
    ) -> str:
        context = getattr(ctx, "context", None)
        value = runtime_extra.get(name, getattr(context, name, None))
        return str(value or "").strip()

    @staticmethod
    def _bind_outer_route(ctx: AgentCallbackContext) -> str | None:
        """Copy the caller context, then bind one token only to this invoke."""

        inputs = getattr(ctx, "inputs", None)
        run_context = getattr(inputs, "run_context", None)
        if inputs is None or run_context is None:
            return None
        if isinstance(run_context, Mapping):
            if not isinstance(run_context, dict):
                return None
            extra = run_context.get("extra")
            if not isinstance(extra, Mapping):
                return None
            copied_context = dict(run_context)
            copied_extra = dict(extra)
            copied_context["extra"] = copied_extra
        else:
            extra = getattr(run_context, "extra", None)
            if not isinstance(extra, Mapping):
                return None
            try:
                copied_context = copy(run_context)
            except Exception:
                return None
            if copied_context is run_context:
                return None
            copied_extra = dict(extra)
            try:
                setattr(copied_context, "extra", copied_extra)
            except Exception:
                return None
        token = uuid4().hex
        copied_extra[_INVOKE_ROUTE_KEY] = token
        inputs.run_context = copied_context
        return token

    @classmethod
    def _route_token_for_ctx(cls, ctx: AgentCallbackContext) -> str:
        token = cls._runtime_extra(ctx).get(_INVOKE_ROUTE_KEY)
        return str(token or "").strip()

    def _claim_paused_state(
        self,
        scope: _InvokeScope,
        query: Any,
        outer_context_id: int,
        route_token: str,
    ) -> _SymphonyInvokeState | None:
        if not self._is_valid_interactive_answer(query):
            return None
        component_ids = self._interactive_component_ids(query)
        with self._paused_states_lock:
            matches = [
                (key, state)
                for key, state in self._paused_states.items()
                if key.session_id == scope.session_id
                and key.capture_mode == scope.capture_mode
                and key.owner_id == scope.owner_id
                and key.component_id in component_ids
            ]
            if (
                len(component_ids) != 1
                or len(matches) != 1
                or not matches[0][1].valid
                or matches[0][1].generation != self._scope_generations.get(scope)
            ):
                self._invalidate_scope_locked(scope)
                return None
            _, state = matches[0]
            self._remove_state_locked(state)
            # The resumed state becomes active while still under the same
            # lock that removed its paused key. A competing new invocation
            # will therefore invalidate this state rather than letting a
            # detached claimant re-attach after that new invocation.
            if scope in self._active_states:
                self._invalidate_scope_locked(scope)
                state.valid = False
                return None
            self._active_states[scope] = state
            state.route_token = route_token
            self._active_route_states[route_token] = state
            self._outer_states[outer_context_id] = state
            return state

    def _begin_conflict_quarantine_locked(
        self, scope: _InvokeScope, outer_context_id: int
    ) -> bool:
        """Fail closed for overlapping outer invokes in one route."""

        quarantined = self._quarantined_scopes.get(scope)
        if quarantined is not None:
            quarantined.add(outer_context_id)
            return True
        outstanding = {
            context_id
            for context_id, state in self._outer_states.items()
            if (
                state.session_id == scope.session_id
                and state.capture_mode == scope.capture_mode
                and state.owner_id == scope.owner_id
            )
        }
        if not outstanding:
            return False
        outstanding.add(outer_context_id)
        self._quarantined_scopes[scope] = outstanding
        self._invalidate_scope_locked(scope)
        return True

    def _finish_quarantine_locked(
        self, scope: _InvokeScope, outer_context_id: int
    ) -> None:
        quarantined = self._quarantined_scopes.get(scope)
        if quarantined is None:
            return
        quarantined.discard(outer_context_id)
        if not quarantined:
            self._quarantined_scopes.pop(scope, None)

    def _cleanup_scope_generation_locked(self, scope: _InvokeScope) -> None:
        """Drop finished scopes without weakening live pause validation."""

        if scope in self._active_states or scope in self._quarantined_scopes:
            return
        if any(
            key.session_id == scope.session_id
            and key.capture_mode == scope.capture_mode
            and key.owner_id == scope.owner_id
            for key in self._paused_states
        ):
            return
        if any(
            state.session_id == scope.session_id
            and state.capture_mode == scope.capture_mode
            and state.owner_id == scope.owner_id
            for state in self._outer_states.values()
        ):
            return
        self._scope_generations.pop(scope, None)

    def _pause_state(
        self, state: _SymphonyInvokeState, component_ids: tuple[str, ...]
    ) -> None:
        keys = tuple(
            _PausedInvokeKey(
                state.session_id, state.capture_mode, state.owner_id, component_id
            )
            for component_id in component_ids
        )
        with self._paused_states_lock:
            scope = _InvokeScope(state.session_id, state.capture_mode, state.owner_id)
            current = self._active_states.get(scope)
            if (
                not state.valid
                or current is not state
                or state.generation != self._scope_generations.get(scope)
            ):
                return
            if any(
                key in self._paused_states and self._paused_states[key] is not state
                for key in keys
            ):
                self._invalidate_scope_locked(scope)
                return
            self._remove_state_locked(state)
            self._active_states.pop(scope, None)
            self._active_route_states.pop(state.route_token, None)
            for key in keys:
                self._paused_states[key] = state

    def _invalidate_state_scope(self, state: _SymphonyInvokeState) -> None:
        scope = _InvokeScope(state.session_id, state.capture_mode, state.owner_id)
        with self._paused_states_lock:
            is_current = self._active_states.get(scope) is state or any(
                paused is state for paused in self._paused_states.values()
            )
            if is_current:
                self._invalidate_scope_locked(scope)
            else:
                # A newer turn owns this route. A late callback may only
                # invalidate its own detached state, never that newer turn.
                state.valid = False

    def _invalidate_scope_locked(self, scope: _InvokeScope) -> int:
        """Invalidate all same-route state before starting/rejecting a turn."""

        generation = self._scope_generations.get(scope, 0) + 1
        self._scope_generations[scope] = generation
        current = self._active_states.pop(scope, None)
        if current is not None:
            current.valid = False
            self._active_route_states.pop(current.route_token, None)
        for state in self._outer_states.values():
            if (
                state.session_id == scope.session_id
                and state.capture_mode == scope.capture_mode
                and state.owner_id == scope.owner_id
            ):
                state.valid = False
                self._active_route_states.pop(state.route_token, None)
        for key in tuple(self._paused_states):
            if (
                key.session_id == scope.session_id
                and key.capture_mode == scope.capture_mode
                and key.owner_id == scope.owner_id
            ):
                state = self._paused_states.pop(key)
                state.valid = False
        return generation

    def _remove_state_locked(self, state: _SymphonyInvokeState) -> None:
        for key, value in tuple(self._paused_states.items()):
            if value is state:
                self._paused_states.pop(key, None)

    def _active_state_for_ctx(
        self, ctx: AgentCallbackContext
    ) -> _SymphonyInvokeState | None:
        route_token = self._route_token_for_ctx(ctx)
        if not route_token:
            return None
        scope = self._scope_for_ctx(ctx)
        if scope is None:
            return None
        with self._paused_states_lock:
            state = self._active_route_states.get(route_token)
            if (
                state is None
                or not state.valid
                or self._active_states.get(scope) is not state
                or state.route_token != route_token
            ):
                return None
            return state

    def _remove_active_state(self, state: _SymphonyInvokeState) -> None:
        scope = _InvokeScope(state.session_id, state.capture_mode, state.owner_id)
        with self._paused_states_lock:
            if self._active_states.get(scope) is state:
                self._active_states.pop(scope, None)
                self._active_route_states.pop(state.route_token, None)

    @classmethod
    def _is_valid_interactive_answer(cls, value: Any) -> bool:
        try:
            from openjiuwen.core.session.interaction.interactive_input import (
                InteractiveInput,
            )

            if not isinstance(value, InteractiveInput) or value.raw_inputs is not None:
                return False
        except Exception:
            return False
        user_inputs = getattr(value, "user_inputs", None)
        if not isinstance(user_inputs, Mapping) or len(user_inputs) != 1:
            return False
        payload = next(iter(user_inputs.values()))
        answers = payload.get("answers") if isinstance(payload, Mapping) else None
        return (
            isinstance(answers, Mapping)
            and bool(answers)
            and all(
                isinstance(question, str)
                and bool(question.strip())
                and isinstance(answer, str)
                for question, answer in answers.items()
            )
        )

    @staticmethod
    def _interactive_component_ids(value: Any) -> tuple[str, ...]:
        user_inputs = getattr(value, "user_inputs", None)
        if not isinstance(user_inputs, Mapping):
            return ()
        component_ids = tuple(
            str(component_id or "").strip() for component_id in user_inputs
        )
        if not component_ids or any(not component_id for component_id in component_ids):
            return ()
        return component_ids if len(set(component_ids)) == len(component_ids) else ()

    @classmethod
    def _interrupt_component_ids(cls, result: Any) -> tuple[str, ...]:
        if (
            not isinstance(result, Mapping)
            or str(result.get("result_type") or "").lower() != "interrupt"
        ):
            return ()
        component_present, component_ids = cls._normalized_interrupt_ids(
            result, "component_ids"
        )
        interrupt_present, interrupt_ids = cls._normalized_interrupt_ids(
            result, "interrupt_ids"
        )
        if not component_present and not interrupt_present:
            return ()
        if component_ids is None or interrupt_ids is None:
            return ()
        if component_present and interrupt_present:
            return component_ids if component_ids == interrupt_ids else ()
        return component_ids if component_present else interrupt_ids

    @staticmethod
    def _normalized_interrupt_ids(
        result: Mapping[str, Any], field_name: str
    ) -> tuple[bool, tuple[str, ...] | None]:
        if field_name not in result:
            return False, ()
        values = result.get(field_name)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return True, None
        ids = tuple(str(value or "").strip() for value in values)
        if not ids or any(not value for value in ids) or len(set(ids)) != len(ids):
            return True, None
        return True, ids

    def _remember_resume_answers(self, state: _SymphonyInvokeState, query: Any) -> None:
        user_inputs = getattr(query, "user_inputs", None)
        if not isinstance(user_inputs, Mapping) or len(user_inputs) != 1:
            return
        payload = next(iter(user_inputs.values()))
        if not isinstance(payload, Mapping):
            return
        answers = payload.get("answers")
        if not isinstance(answers, Mapping):
            return
        state.answered = True
        for question, answer in answers.items():
            if len(state.answers) >= _MAX_RESUME_ANSWERS:
                break
            normalized = self._normalized_answer(question, answer)
            if normalized is not None:
                state.answers.append(normalized)

    def _normalized_answer(self, question: Any, answer: Any) -> str | None:
        label = str(question or "").strip()
        if not label or _SENSITIVE_INPUT_NAME.search(label):
            return None
        safe_value = self._safe_json_value(answer)
        if safe_value is None or self._contains_sensitive_key(safe_value):
            return None
        serialized = json.dumps(safe_value, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > _MAX_RESUME_ANSWER_CHARS:
            serialized = '"[truncated]"'
        return f"{label[:256]}: {serialized}"

    def _safe_json_value(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return (
                value
                if value == value and value not in {float("inf"), float("-inf")}
                else None
            )
        if isinstance(value, list):
            return [self._safe_json_value(item) for item in value[:32]]
        if isinstance(value, Mapping):
            return {
                str(key)[:128]: self._safe_json_value(item)
                for key, item in list(value.items())[:32]
                if isinstance(key, (str, int, float, bool))
            }
        return None

    def _contains_sensitive_key(self, value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(
                _SENSITIVE_INPUT_NAME.search(str(key))
                or self._contains_sensitive_key(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(self._contains_sensitive_key(item) for item in value)
        return False

    @staticmethod
    def _resume_query(state: _SymphonyInvokeState) -> str:
        if not state.answers:
            if state.answered:
                return (
                    f"{state.original_query}\n\n补充信息：\n"
                    "- 用户已回答，但内容因安全策略未纳入。"
                )
            return state.original_query
        return f"{state.original_query}\n\n补充信息：\n" + "\n".join(
            f"- {answer}" for answer in state.answers
        )

    def _candidate_skill_ids(self, inputs: ToolCallInputs) -> list[str]:
        values = self._tool_args(inputs).get("candidate_skill_ids")
        if not isinstance(values, list):
            return []
        ordered: list[str] = []
        for value in values:
            candidate = str(value or "").strip()
            if candidate and candidate not in ordered:
                ordered.append(candidate)
        return ordered

    @staticmethod
    def _planned_graph_status(result: Any) -> tuple[str, Mapping[str, Any] | None]:
        if not isinstance(result, Mapping):
            return "", None
        planned_graph = result.get("planned_graph")
        if not isinstance(planned_graph, Mapping):
            return "", None
        graph = planned_graph.get("graph")
        if not isinstance(graph, Mapping):
            return "", None
        metadata = graph.get("metadata")
        if not isinstance(metadata, Mapping):
            return "", None
        return str(metadata.get("status") or "").strip().lower(), metadata

    @staticmethod
    def _is_cancelled_or_error_result(result: Any) -> bool:
        if not isinstance(result, Mapping):
            return False
        status = str(result.get("status") or "").lower()
        result_type = str(result.get("result_type") or "").lower()
        return (
            result.get("success") is False
            or status in {"cancelled", "canceled", "error", "failed", "failure"}
            or result_type in {"cancelled", "canceled", "error"}
        )

    def _bind_request_model(self, ctx: AgentCallbackContext) -> None:
        if not self._is_graph_tool_call(ctx):
            return
        reference = self._request_model_reference(ctx)
        if not reference:
            return
        # AbilityManager executes parallel tool calls in separate asyncio
        # tasks. Store the reset token on the per-tool callback context while
        # the ContextVar itself keeps each task's selected model isolated.
        setattr(ctx, _REQUEST_LLM_TOKEN_ATTR, bind_request_llm_config(reference))

    @staticmethod
    def _reset_request_model(ctx: AgentCallbackContext) -> None:
        token = getattr(ctx, _REQUEST_LLM_TOKEN_ATTR, None)
        if token is None:
            return
        reset_request_llm_config(token)
        setattr(ctx, _REQUEST_LLM_TOKEN_ATTR, None)

    @staticmethod
    def _request_model_reference(ctx: AgentCallbackContext) -> str:
        run_context = ctx.extra.get("run_context")
        if isinstance(run_context, Mapping):
            extra = run_context.get("extra")
        else:
            extra = getattr(run_context, "extra", None)
        if not isinstance(extra, Mapping):
            return ""
        return str(extra.get(SYMPHONY_LLM_CONFIG_REF_KEY) or "").strip()

    def _sync_orchestration_guidance(self, ctx: AgentCallbackContext) -> None:
        builder = self.system_prompt_builder
        if builder is None:
            return
        if not self._has_compose_tool(ctx) or not self._orchestration_enabled():
            builder.remove_section(self.SECTION_NAME)
            return

        language = getattr(builder, "language", "cn") or "cn"
        builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={
                    language: self._build_orchestration_guidance(
                        resumed=self._resume_is_pending(ctx)
                    )
                },
                priority=self.SECTION_PRIORITY,
            )
        )

    def _inject_shortlisted_candidates(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        if self._tool_name(ctx.inputs) != self.COMPOSE_TOOL_NAME:
            return

        args = self._tool_args(ctx.inputs)
        if "candidate_skill_ids" in args:
            return
        shortlisted = self._shortlisted_skill_ids(ctx)
        if not shortlisted:
            return

        args["candidate_skill_ids"] = shortlisted
        ctx.inputs.tool_args = args
        if ctx.inputs.tool_call is not None:
            ctx.inputs.tool_call.arguments = args

    def _terminate_structured_graph_build_timeout(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        if not self._is_graph_tool_call(ctx):
            return
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        result = inputs.tool_result
        if (
            not isinstance(result, dict)
            or result.get("reason") != "graph_build_timeout"
        ):
            return
        if (
            result.get("direct_display") is True
            and result.get("followup_action") == "manual_graph_build"
        ):
            return
        self._terminate_graph_build_timeout(ctx, result)

    def _terminate_structured_graph_preparing(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        if not self._is_graph_tool_call(ctx):
            return
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        result = inputs.tool_result
        if not isinstance(result, dict) or result.get("reason") != "graph_preparing":
            return
        if (
            result.get("direct_display") is True
            and result.get("followup_action") == "wait_graph_build"
        ):
            return

        payload = dict(result)
        payload.update(
            {
                "success": False,
                "reason": "graph_preparing",
                "retryable": False,
                "build_status": "running",
                "direct_display": True,
                "continue_after_display": False,
                "followup_action": "wait_graph_build",
                "content": self._graph_preparing_content(),
            }
        )
        inputs.tool_result = payload
        # Reuse the existing invocation marker that removes both graph tools.
        ctx.extra[_GRAPH_BUILD_TIMEOUT_EXTRA_KEY] = True
        ctx.request_force_finish(
            {"output": payload["content"], "result_type": "answer"}
        )

    def _terminate_graph_build_timeout(
        self,
        ctx: AgentCallbackContext,
        result: dict[str, Any],
    ) -> None:
        payload = dict(result)
        payload.update(
            {
                "success": False,
                "reason": "graph_build_timeout",
                "timed_out": True,
                "retryable": False,
                "direct_display": True,
                "continue_after_display": False,
                "followup_action": "manual_graph_build",
                "content": self._manual_graph_build_content(),
            }
        )
        if isinstance(ctx.inputs, ToolCallInputs):
            ctx.inputs.tool_result = payload
        ctx.extra[_GRAPH_BUILD_TIMEOUT_EXTRA_KEY] = True
        ctx.request_force_finish(
            {"output": payload["content"], "result_type": "answer"}
        )

    def _remove_graph_tools_after_timeout(self, ctx: AgentCallbackContext) -> None:
        if not ctx.extra.get(_GRAPH_BUILD_TIMEOUT_EXTRA_KEY):
            return
        if not isinstance(ctx.inputs, ModelCallInputs):
            return
        tools = ctx.inputs.tools
        if not isinstance(tools, list):
            return
        ctx.inputs.tools = [
            tool
            for tool in tools
            if self._model_tool_name(tool) not in _GRAPH_TOOL_NAMES
        ]

    def _manual_graph_build_content(self) -> str:
        language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
        return _MANUAL_GRAPH_BUILD_CONTENT["en" if language == "en" else "cn"]

    def _graph_preparing_content(self) -> str:
        language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
        return _GRAPH_PREPARING_CONTENT["en" if language == "en" else "cn"]

    @classmethod
    def _is_graph_tool_call(cls, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        return (
            isinstance(inputs, ToolCallInputs)
            and cls._tool_name(inputs) in _GRAPH_TOOL_NAMES
        )

    @classmethod
    def _is_compose_tool_call(cls, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        return (
            isinstance(inputs, ToolCallInputs)
            and cls._tool_name(inputs) == cls.COMPOSE_TOOL_NAME
        )

    @classmethod
    def _is_outer_graph_timeout(cls, ctx: AgentCallbackContext) -> bool:
        exception = ctx.exception
        if not isinstance(exception, AbilityExecutionError):
            return False
        if not isinstance(exception.__cause__, TimeoutError):
            return False
        match = _ABILITY_MANAGER_TIMEOUT_PATTERN.fullmatch(
            cls._exception_message(exception)
        )
        return match is not None and match.group(1) == cls._tool_name(ctx.inputs)

    @staticmethod
    def _exception_message(exception: AbilityExecutionError) -> str:
        tool_message = exception.tool_message
        content = getattr(tool_message, "content", None)
        return content.strip() if isinstance(content, str) else ""

    @classmethod
    def _outer_timeout_payload(cls, ctx: AgentCallbackContext) -> dict[str, Any]:
        tool_name = cls._tool_name(ctx.inputs)
        operation = "plan" if tool_name == cls.COMPOSE_TOOL_NAME else "refresh_graph"
        exception = ctx.exception
        message = cls._exception_message(exception)
        payload: dict[str, Any] = {
            "success": False,
            "reason": "graph_build_timeout",
            "timed_out": True,
            "retryable": False,
            "operation": operation,
            "detail": message,
        }
        match = _ABILITY_MANAGER_TIMEOUT_PATTERN.fullmatch(message)
        if match:
            payload["timeout_s"] = float(match.group(2))
        return payload

    def _remember_successful_skill_view(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        if not self._is_skill_tool(self._tool_name(ctx.inputs)):
            return
        if ctx.exception is not None:
            return
        if self._tool_result_failed(ctx.inputs.tool_result):
            return

        args = self._tool_args(ctx.inputs)
        skill_name = str(args.get("skill_name") or args.get("skillName") or "").strip()
        if not skill_name:
            return
        shortlisted = self._shortlisted_skill_ids(ctx)
        if skill_name not in shortlisted:
            shortlisted.append(skill_name)
        ctx.extra[_SHORTLIST_EXTRA_KEY] = shortlisted

    def _orchestration_enabled(self) -> bool:
        try:
            from jiuwenswarm.symphony.config import load_symphony_config

            config_base = self._resolve_config_base()
            config = (
                load_symphony_config()
                if config_base is None
                else load_symphony_config(config_base)
            )
        except Exception:
            return False
        return bool(config.enabled)

    def _resume_is_pending(self, ctx: AgentCallbackContext) -> bool:
        state = self._active_state_for_ctx(ctx)
        return state is not None and state.pending_recompose

    @staticmethod
    def _build_orchestration_guidance(*, resumed: bool = False) -> str:
        resume_guidance = (
            """
This invocation resumes a Symphony clarification. Before executing any
`skill_tool`, call `symphony_compose_graph` exactly once. The rail keeps the
original task, normalized user answers, and the original selected candidate
shortlist. Do not replace that shortlist or retry `needs_input` in this loop.
"""
            if resumed
            else ""
        )
        return f"""
## Skill Orchestration Contract

Before executing Skills or answering, you MUST call `symphony_compose_graph`
with the original user task as `query` when ANY of these conditions is true:

1. The user explicitly requests using, selecting, combining, or orchestrating
   Skills, including requests that mention skill(s) or 技能.
2. The task requires two or more specialized capabilities or an ordered
   toolchain.
3. You have identified, inspected, selected, invoked, or recommended any
   installed Skill for the task.

Calling `skill_branch_explore` creates a mandatory orchestration follow-up. After
calling it, select only the few Skills relevant to the original user task and
call `symphony_compose_graph` before executing any Skill or returning a final
answer. Pass the selected Skills' exact identifiers or names as
`candidate_skill_ids`; never pass every Skill returned by exploration. If no
candidate can be selected confidently, still call `symphony_compose_graph`
with the original query and omit `candidate_skill_ids`. Use retrieval metadata
directly: between `skill_branch_explore` and `symphony_compose_graph`, do not call
`skill_tool`, `read_file`, or read any SKILL.md.

Do not choose the execution chain yourself; the orchestration tool determines
Skill ordering and graph composition. It returns `planned_graph`; read
`planned_graph.graph.metadata.status`, `planned_graph.graph.nodes`, and
`planned_graph.graph.edges`, then decide whether to execute, request more
information, or take other appropriate next steps. Do not present a planning
rendering for confirmation. When status is `ready`, use the graph to choose one
currently executable Skill, read only that Skill's SKILL.md immediately before
executing it, complete its execution, and then read the next executable Skill.
Do not preload all selected Skill instructions. When status is `needs_input` or
`no_plan`, do not read Skills merely for orchestration.

If either graph tool returns `graph_build_timeout` or `manual_graph_build`, do
not call `symphony_compose_graph` or `symphony_refresh_graph` again in the
current round. Tell the user to build the graph manually instead.

Skip skill orchestration only when none of the three trigger conditions is true
and `skill_branch_explore` was not called in the current round.
{resume_guidance}"""

    @staticmethod
    def _tool_name(inputs: ToolCallInputs) -> str:
        return str(
            inputs.tool_name or getattr(inputs.tool_call, "name", "") or ""
        ).strip()

    @staticmethod
    def _is_skill_tool(name: str) -> bool:
        return str(name or "").strip().lower() == "skill_tool"

    @staticmethod
    def _tool_args(inputs: ToolCallInputs) -> dict[str, Any]:
        if isinstance(inputs.tool_args, dict):
            return dict(inputs.tool_args)
        raw_args = getattr(inputs.tool_call, "arguments", None)
        if isinstance(raw_args, dict):
            return dict(raw_args)
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except (TypeError, ValueError):
                return {}
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        return {}

    @staticmethod
    def _tool_result_failed(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("success") is False:
            return True
        return str(result.get("status") or "").strip().lower() == "error"

    @staticmethod
    def _shortlisted_skill_ids(ctx: AgentCallbackContext) -> list[str]:
        values = ctx.extra.get(_SHORTLIST_EXTRA_KEY)
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    @classmethod
    def _has_compose_tool(cls, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not tools:
            return False
        return any(
            cls._model_tool_name(tool) == cls.COMPOSE_TOOL_NAME for tool in tools
        )

    @staticmethod
    def _model_tool_name(tool: Any) -> str:
        if isinstance(tool, dict):
            function = tool.get("function")
            if isinstance(function, dict):
                return str(function.get("name", "") or "")
            return str(tool.get("name", "") or "")
        function = getattr(tool, "function", None)
        if isinstance(function, dict):
            return str(function.get("name", "") or "")
        function_name = getattr(function, "name", None)
        if function_name is not None:
            return str(function_name)
        name = getattr(tool, "name", None)
        if name is not None:
            return str(name)
        card = getattr(tool, "card", None)
        return str(getattr(card, "name", "") or "")

    def _resolve_config_base(self) -> dict[str, Any] | None:
        if callable(self._config_base):
            return self._config_base()
        return self._config_base


__all__ = ["SymphonyOrchestrationRail"]
