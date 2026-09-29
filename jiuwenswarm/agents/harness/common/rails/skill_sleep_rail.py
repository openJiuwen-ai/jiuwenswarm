# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rail: count per-skill *usage* (not loads) and trigger skill sleep offline training.

Usage = one increment per user task per skill (deduped), when that skill was
employed via work tools. ``skill_tool`` only binds attribution (load ≠ usage).

A user task may span several invokes when it pauses on HITL
(permission / confirm / ask_user). The in-task used set survives those
pauses and is flushed once when the task actually ends (or, if the task is
abandoned, when the next new task starts).

The ``skill_tool`` fallback attribution is task-scoped as well: it is dropped
when the task ends or a new task starts (``skill_complete`` is often never
called). ``last_skills.json`` therefore only carries tasks paused mid-flight
so they survive a rail rebuild / restart before being resumed. Several rails
(one per session adapter) share that file, so each write merges only the
sessions this rail touched; ``forget_session`` drops a session on teardown.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import portalocker
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    BaseInterruptionState,
)
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
    _get_arg,
    _is_interrupt_resume_invoke,
    get_session_active_skill,
    resolve_skill_session_id,
)
from jiuwenswarm.agents.harness.common.skill_sleep.counter import SkillCallCounter
from jiuwenswarm.agents.harness.common.skill_sleep.runner import SkillSleepRunner

logger = logging.getLogger(__name__)

# Loads / lifecycle / retrieval / bookkeeping — not counted as skill "usage".
_NON_USAGE_TOOLS = frozenset(
    {
        "skill_tool",
        "skill_complete",
        "todo_create",
        "todo_modify",
        "todo_list",
        "ask_user",
        # skill_toolkit: search / install lifecycle
        "search_skill",
        "install_skill",
        "uninstall_skill",
        # skill_retrieval: browse / index (load path, not work)
        "skill_index_build",
        "skill_branch_explore",
        "skill_branch_peek",
    }
)

_LAST_SKILLS_PERSIST_DELAY_SEC = 1.0
_LAST_SKILLS_MAX_ENTRIES = 512
_LAST_SKILLS_TTL_SEC = 7 * 24 * 3600
_LAST_SKILLS_FILE_LOCK_TIMEOUT_SEC = 5.0

_LastSkillEntries = dict[str, tuple[str, float]]


def _parse_last_skills(raw: Any, now: float) -> _LastSkillEntries:
    """``{sid: {"skill", "ts"}}``; legacy ``{sid: skill}`` values get *now*."""
    entries: _LastSkillEntries = {}
    if not isinstance(raw, dict):
        return entries
    for key, value in raw.items():
        sid = str(key or "").strip()
        ts = now
        if isinstance(value, dict):
            name = str(value.get("skill") or "").strip()
            try:
                ts = float(value.get("ts") or now)
            except (TypeError, ValueError):
                ts = now
        else:
            name = str(value or "").strip()
        if sid and name:
            entries[sid] = (name, ts)
    return entries


def _prune_last_skills(entries: _LastSkillEntries, now: float) -> _LastSkillEntries:
    live = {
        sid: entry
        for sid, entry in entries.items()
        if (now - entry[1]) <= _LAST_SKILLS_TTL_SEC
    }
    if len(live) <= _LAST_SKILLS_MAX_ENTRIES:
        return live
    newest = sorted(live.items(), key=lambda item: item[1][1], reverse=True)
    return dict(newest[:_LAST_SKILLS_MAX_ENTRIES])


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


def _invoke_paused_on_interrupt(ctx: AgentCallbackContext) -> bool:
    """True when this (outer DeepAgent) invoke ended waiting on HITL.

    Streaming single-round invokes never surface ``result_type=interrupt`` on
    the outer ``InvokeInputs`` (only ``answer`` chunks are folded into it), so
    the pending interruption state the ReActAgent commits to the session is
    the authoritative signal; resume / abandon clear it.
    """
    exc = getattr(ctx, "exception", None)
    if exc is not None and type(exc).__name__ in {
        "ToolInterruptException",
        "ToolInterruptError",
    }:
        return True
    result = getattr(getattr(ctx, "inputs", None), "result", None)
    if isinstance(result, dict) and result.get("result_type") == "interrupt":
        return True
    getter = getattr(getattr(ctx, "session", None), "get_state", None)
    if callable(getter):
        try:
            return isinstance(getter(INTERRUPTION_KEY), BaseInterruptionState)
        except Exception:
            logger.debug("[SkillSleepRail] read interruption state failed", exc_info=True)
    return False


def _should_preserve_task(ctx: AgentCallbackContext) -> bool:
    """HITL resume invokes continue the same user task (no new usage window).

    ``ctx.extra`` is rebuilt empty per invoke; the chat.send source only lives
    in ``inputs.run_context.extra``, which ``_is_interrupt_resume_invoke`` reads.
    """
    return _is_interrupt_resume_invoke(ctx)


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

    # Higher runs first, so this fires before SkillActiveStateRail (25); the
    # counting logic reads only its own state and inputs.run_context.extra.
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
        # session_id -> last skill from skill_tool in the current user task
        # (until skill_complete or the task ends)
        self._last_skill: dict[str, str] = {}
        self._last_skill_ts: dict[str, float] = {}
        self._last_skills_path = Path(
            last_skills_path
            if last_skills_path is not None
            else (counter.path.parent / "last_skills.json")
        )
        self._persist_lock = threading.Lock()
        self._dirty_sessions: set[str] = set()
        self._persist_timer: threading.Timer | None = None
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

    def _last_skills_file_lock(self) -> portalocker.Lock:
        path = self._last_skills_path
        path.parent.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(
            str(path.with_suffix(path.suffix + ".lock")),
            mode="a+",
            timeout=_LAST_SKILLS_FILE_LOCK_TIMEOUT_SEC,
        )

    def _read_last_skills_unlocked(self, now: float) -> _LastSkillEntries:
        path = self._last_skills_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            logger.warning("[SkillSleepRail] corrupt %s; ignoring", path, exc_info=True)
            return {}
        return _parse_last_skills(raw, now)

    def _load_last_skills(self) -> None:
        if not self._last_skills_path.exists():
            return
        now = time.time()
        try:
            with self._last_skills_file_lock():
                entries = self._read_last_skills_unlocked(now)
        except Exception:
            logger.warning(
                "[SkillSleepRail] failed to load %s",
                self._last_skills_path,
                exc_info=True,
            )
            return
        entries = _prune_last_skills(entries, now)
        self._last_skill = {sid: name for sid, (name, _ts) in entries.items()}
        self._last_skill_ts = {sid: ts for sid, (_name, ts) in entries.items()}

    def _mark_last_skills_dirty_unlocked(self, session_id: str) -> None:
        self._dirty_sessions.add(session_id)
        if self._persist_timer is not None:
            return
        timer = threading.Timer(
            _LAST_SKILLS_PERSIST_DELAY_SEC,
            self._persist_last_skills,
        )
        timer.daemon = True
        self._persist_timer = timer
        timer.start()

    def _persist_last_skills(self) -> None:
        """Merge this rail's dirty sessions into the shared file."""
        with self._persist_lock:
            if self._persist_timer is not None:
                self._persist_timer.cancel()
                self._persist_timer = None
            if not self._dirty_sessions:
                return
            dirty = set(self._dirty_sessions)
            now = time.time()
            try:
                with self._last_skills_file_lock():
                    merged = self._read_last_skills_unlocked(now)
                    for sid in dirty:
                        name = self._last_skill.get(sid)
                        if name:
                            merged[sid] = (name, self._last_skill_ts.get(sid, now))
                        else:
                            merged.pop(sid, None)
                    merged = _prune_last_skills(merged, now)
                    payload = {
                        sid: {"skill": name, "ts": round(ts, 3)}
                        for sid, (name, ts) in sorted(merged.items())
                    }
                    tmp = self._last_skills_path.with_suffix(
                        self._last_skills_path.suffix + ".tmp"
                    )
                    tmp.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(tmp, self._last_skills_path)
                self._dirty_sessions -= dirty
            except Exception:
                logger.warning(
                    "[SkillSleepRail] failed to persist %s",
                    self._last_skills_path,
                    exc_info=True,
                )
            live = _prune_last_skills(
                {
                    sid: (name, self._last_skill_ts.get(sid, now))
                    for sid, name in self._last_skill.items()
                },
                now,
            )
            self._last_skill = {sid: name for sid, (name, _ts) in live.items()}
            self._last_skill_ts = {sid: ts for sid, (_name, ts) in live.items()}

    def _remember_skill(self, session_id: str, skill_name: str) -> None:
        name = str(skill_name or "").strip()
        if not session_id or not name:
            return
        with self._persist_lock:
            self._last_skill[session_id] = name
            self._last_skill_ts[session_id] = time.time()
            self._mark_last_skills_dirty_unlocked(session_id)

    def _forget_skill(self, session_id: str, skill_name: str = "") -> None:
        name = str(skill_name or "").strip()
        if not session_id:
            return
        with self._persist_lock:
            current = self._last_skill.get(session_id, "")
            if not current or (name and current != name):
                return
            self._last_skill.pop(session_id, None)
            self._last_skill_ts.pop(session_id, None)
            self._mark_last_skills_dirty_unlocked(session_id)

    def forget_session(self, session_id: str) -> None:
        """Session teardown: count unfinished task usage, then drop attribution."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        try:
            self._end_task(sid)
        finally:
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
            # New user task. A leftover set means the previous task paused on
            # HITL and was abandoned; it still happened, so count it now.
            self._end_task(self._session_id(ctx))
        except Exception:
            logger.warning("[SkillSleepRail] before_invoke failed", exc_info=True)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        try:
            self._note_tool_usage(ctx)
        except Exception:
            logger.warning("[SkillSleepRail] after_tool_call failed", exc_info=True)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        try:
            if _invoke_paused_on_interrupt(ctx):
                return
            self._end_task(self._session_id(ctx))
        except Exception:
            logger.warning("[SkillSleepRail] after_invoke failed", exc_info=True)
        finally:
            # Flush deferred last_skills.json at end of user turn.
            try:
                self._persist_last_skills()
            except Exception:
                logger.warning(
                    "[SkillSleepRail] after_invoke persist failed", exc_info=True
                )

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

    def _end_task(self, session_id: str) -> None:
        self._flush_task_usage(session_id)
        self._forget_skill(session_id)

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
            started = self._runner.try_start(skill_name, session_id=session_id)
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
