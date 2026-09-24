# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rail: count per-skill *usage* (not loads) and trigger skill sleep offline training.

Usage = one increment per user task per skill (deduped), when that skill was
employed via work tools. ``skill_tool`` only binds attribution (load ≠ usage).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    is_interrupt_resume_source,
)
from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
    _CHAT_SEND_SOURCE_EXTRA_KEY,
    _PRESERVE_SKILL_ACTIVE_EXTRA_KEY,
    _get_arg,
    get_session_active_skill,
    resolve_skill_session_id,
    should_preserve_skill_active_from_params,
)
from jiuwenswarm.agents.harness.common.skill_sleep.counter import SkillCallCounter
from jiuwenswarm.agents.harness.common.skill_sleep.runner import SkillSleepRunner

logger = logging.getLogger(__name__)

# Loads / lifecycle / bookkeeping — not counted as skill "usage".
_NON_USAGE_TOOLS = frozenset(
    {
        "skill_tool",
        "skill_complete",
        "todo_create",
        "todo_modify",
        "todo_list",
        "ask_user",
    }
)


def _tool_call_failed(ctx: AgentCallbackContext) -> bool:
    if getattr(ctx, "exception", None) is not None:
        return True
    inputs = ctx.inputs
    if not isinstance(inputs, ToolCallInputs):
        return True
    result = inputs.tool_result
    if result is None:
        return True
    name = type(result).__name__
    if name in {"ToolInterruptException", "ToolInterruptError"}:
        return True
    return False


def _is_interrupt_ctx(ctx: AgentCallbackContext) -> bool:
    exc = getattr(ctx, "exception", None)
    if exc is not None and type(exc).__name__ in {
        "ToolInterruptException",
        "ToolInterruptError",
    }:
        return True
    inputs = getattr(ctx, "inputs", None)
    result = getattr(inputs, "tool_result", None) if inputs is not None else None
    if result is not None and type(result).__name__ in {
        "ToolInterruptException",
        "ToolInterruptError",
    }:
        return True
    return False


def _should_preserve_task(ctx: AgentCallbackContext) -> bool:
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict) and extra.get(_PRESERVE_SKILL_ACTIVE_EXTRA_KEY) is True:
        return True
    inputs = getattr(ctx, "inputs", None)
    params = getattr(inputs, "inputs", None) if inputs is not None else None
    if isinstance(params, dict) and should_preserve_skill_active_from_params(params):
        return True
    if isinstance(extra, dict):
        source = str(extra.get(_CHAT_SEND_SOURCE_EXTRA_KEY) or "").strip()
        if is_interrupt_resume_source(source):
            return True
    return False


def _resolve_skill_name_from_tool(ctx: AgentCallbackContext) -> str:
    inputs = ctx.inputs
    if not isinstance(inputs, ToolCallInputs):
        return ""
    tool_msg = getattr(inputs, "tool_msg", None)
    meta = getattr(tool_msg, "metadata", None) or {}
    if isinstance(meta, dict) and meta.get("is_directory_listing"):
        return ""
    if isinstance(meta, dict):
        from_meta = str(meta.get("skill_name") or "").strip()
        if from_meta:
            return from_meta
    tool_call = getattr(inputs, "tool_call", None)
    if tool_call is None:
        return ""
    return (_get_arg(tool_call, "skill_name", "") or "").strip()


def _resolve_active_skill_name(ctx: AgentCallbackContext, session_id: str) -> str:
    """Prefer SkillUseRail session binding, then SkillActiveStateRail."""
    session = getattr(ctx, "session", None)
    try:
        from openjiuwen.harness.rails.skills.skill_use_rail import get_current_skill_name

        name = str(get_current_skill_name(session) or "").strip()
        if name:
            return name
    except Exception:
        logger.debug("[SkillSleepRail] get_current_skill_name failed", exc_info=True)
    return str(get_session_active_skill(session_id) or "").strip()


class SkillSleepRail(DeepAgentRail):
    """Count one usage per user-task per skill; ignore ``skill_tool`` loads."""

    # After SkillActiveStateRail (25) so active-skill state is already updated.
    priority = 26

    def __init__(
        self,
        *,
        counter: SkillCallCounter,
        runner: SkillSleepRunner,
        call_threshold: int = 20,
        last_skills_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self._counter = counter
        self._runner = runner
        self._call_threshold = max(int(call_threshold), 1)
        # session_id -> skills used in the current user task (deduped)
        self._task_used: dict[str, set[str]] = {}
        # session_id -> last skill from skill_tool (until skill_complete)
        self._last_skill: dict[str, str] = {}
        self._last_skills_path = Path(
            last_skills_path
            if last_skills_path is not None
            else (counter.path.parent / "last_skills.json")
        )
        self._persist_lock = threading.Lock()
        self._load_last_skills()

    @property
    def counter(self) -> SkillCallCounter:
        return self._counter

    @property
    def runner(self) -> SkillSleepRunner:
        return self._runner

    @property
    def call_threshold(self) -> int:
        return self._call_threshold

    def _session_id(self, ctx: AgentCallbackContext) -> str:
        return resolve_skill_session_id(ctx)

    def _load_last_skills(self) -> None:
        path = self._last_skills_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "[SkillSleepRail] failed to load %s", path, exc_info=True
            )
            return
        if not isinstance(raw, dict):
            return
        loaded: dict[str, str] = {}
        for key, value in raw.items():
            sid = str(key or "").strip()
            name = str(value or "").strip()
            if sid and name:
                loaded[sid] = name
        self._last_skill = loaded

    def _persist_last_skills(self) -> None:
        with self._persist_lock:
            try:
                self._last_skills_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    k: v for k, v in sorted(self._last_skill.items()) if k and v
                }
                tmp = self._last_skills_path.with_suffix(
                    self._last_skills_path.suffix + ".tmp"
                )
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, self._last_skills_path)
            except Exception:
                logger.warning(
                    "[SkillSleepRail] failed to persist %s",
                    self._last_skills_path,
                    exc_info=True,
                )

    def _remember_skill(self, session_id: str, skill_name: str) -> None:
        name = str(skill_name or "").strip()
        if not session_id or not name:
            return
        self._last_skill[session_id] = name
        self._persist_last_skills()

    def _forget_skill(self, session_id: str, skill_name: str = "") -> None:
        name = str(skill_name or "").strip()
        if not session_id:
            return
        current = self._last_skill.get(session_id, "")
        if not name or current == name:
            self._last_skill.pop(session_id, None)
            self._persist_last_skills()

    def _mark_task_used(self, session_id: str, skill_name: str) -> None:
        name = str(skill_name or "").strip()
        if not session_id or not name:
            return
        self._task_used.setdefault(session_id, set()).add(name)

    def _attributed_skill(self, ctx: AgentCallbackContext, session_id: str) -> str:
        active = _resolve_active_skill_name(ctx, session_id)
        if active:
            return active
        return str(self._last_skill.get(session_id) or "").strip()

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        try:
            if _should_preserve_task(ctx):
                return
            session_id = self._session_id(ctx)
            # New user task: clear in-task used set; keep persisted attribution.
            self._task_used.pop(session_id, None)
        except Exception:
            logger.warning("[SkillSleepRail] before_invoke failed", exc_info=True)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        try:
            self._note_tool_usage(ctx)
        except Exception:
            logger.warning("[SkillSleepRail] after_tool_call failed", exc_info=True)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        try:
            if _is_interrupt_ctx(ctx):
                return
            self._flush_task_usage(self._session_id(ctx))
        except Exception:
            logger.warning("[SkillSleepRail] after_invoke failed", exc_info=True)

    def _note_tool_usage(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        if _tool_call_failed(ctx):
            return
        tool_name = str(getattr(inputs, "tool_name", "") or "").strip()
        session_id = self._session_id(ctx)

        if tool_name == "skill_tool":
            skill_name = _resolve_skill_name_from_tool(ctx)
            if skill_name:
                self._remember_skill(session_id, skill_name)
            return

        if tool_name == "skill_complete":
            skill_name = _resolve_skill_name_from_tool(ctx)
            self._forget_skill(session_id, skill_name)
            return

        if tool_name in _NON_USAGE_TOOLS:
            return

        active = self._attributed_skill(ctx, session_id)
        if active:
            self._mark_task_used(session_id, active)

    def _flush_task_usage(self, session_id: str) -> None:
        used = self._task_used.pop(session_id, None)
        if not used:
            return
        for skill_name in sorted(used):
            count = self._counter.increment(skill_name)
            logger.info(
                "[SkillSleepRail] task usage skill=%s count=%s threshold=%s",
                skill_name,
                count,
                self._call_threshold,
            )
            if count <= self._call_threshold:
                continue
            started = self._runner.try_start(skill_name)
            if started:
                logger.info(
                    "[SkillSleepRail] triggered sleep skill=%s count_was=%s",
                    skill_name,
                    count,
                )


__all__ = [
    "SkillSleepRail",
    "_resolve_skill_name_from_tool",
    "_tool_call_failed",
]
