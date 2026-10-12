# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Restore read-before-edit state for files the session already saw.

agent-core refuses ``edit_file`` / ``write_file`` unless ``read_file`` read the
file in the current process. In the headless implement driver every
continuation round is a fresh process, so the model's reads from earlier
rounds are forgotten and the edit is refused ("File must be read before
editing"); benchmark runs showed 33–80 such refusals per 20 tasks, over 95% of
them for files read in an earlier round. The desktop hits the same refusal
after an AgentServer restart.

This rail records, per session, every file the agent read, edited or wrote
(size, mtime, content hash). Before an ``edit_file`` / ``write_file``, if
agent-core has not seen the file in this process but this session did, and
the file still has exactly the content the agent saw, the rail calls the
agent's own ``read_file`` tool so agent-core records the read itself. If the
file changed, the session never saw it, or the read policy now denies or asks
for it, nothing happens and agent-core's refusal stands.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.code.read_restore import (
    KIND_EDIT,
    KIND_WRITE,
    SessionReadStore,
    fingerprint,
    read_denied_reason,
    record_change,
    record_read,
)

logger = logging.getLogger(__name__)

_READ_TOOL = "read_file"
_EDIT_TOOL = "edit_file"
_WRITE_TOOL = "write_file"
# Window used to re-read a whole file for write_file; agent-core caps a read at
# 2000 lines and 25k tokens, so smaller windows stay well inside both.
_WRITE_WINDOW_LINES = 500
_MAX_WRITE_WINDOWS = 40

# Files known to be in agent-core's (process-global) read registry with the
# given (mtime_ns, size): read/edited/written in this process or restored.
# agent-core's registry is process-global, so this mirror is too.
_PROCESS_SEEN: dict[str, tuple[int, int]] = {}


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _session_id(ctx: AgentCallbackContext) -> str:
    session = getattr(ctx, "session", None)
    getter = getattr(session, "get_session_id", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return str(value)
    for name in ("session_id", "id"):
        value = getattr(session, name, None)
        if value:
            return str(value)
    return ""


def _resolve_path(file_path: str) -> str | None:
    """agent-core's resolution: absolute paths as given, relative against its cwd."""
    if not isinstance(file_path, str) or not file_path:
        return None
    expanded = os.path.expanduser(file_path)
    if os.path.isabs(expanded) or expanded.startswith(("\\\\", "//")):
        return expanded
    try:
        from openjiuwen.core.sys_operation.cwd import get_cwd

        work_dir = get_cwd()
        return str((Path(work_dir).expanduser().resolve() / expanded).resolve())
    except Exception:  # noqa: BLE001
        return None


def _stat_key(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _result_data(result: Any) -> dict[str, Any] | None:
    if getattr(result, "success", None) is not True:
        return None
    data = getattr(result, "data", None)
    return data if isinstance(data, dict) else None


def _call_skipped(ctx: AgentCallbackContext) -> bool:
    extra = getattr(ctx, "extra", None) or {}
    if extra.get("_skip_tool"):
        return True
    skips = extra.get("_skip_tool_calls")
    call = getattr(getattr(ctx, "inputs", None), "tool_call", None)
    call_id = str(getattr(call, "id", "") or "")
    return bool(isinstance(skips, dict) and call_id and skips.get(call_id))


class ReadRestoreRail(DeepAgentRail):
    """Re-register reads from earlier processes of the same session (see module doc)."""

    # Low priority: runs after permission / policy rails, which may skip the call.
    priority = 15

    def __init__(
        self,
        *,
        config_getter: Callable[[], Mapping[str, Any] | None] | None = None,
        store: SessionReadStore | None = None,
    ) -> None:
        super().__init__()
        self._config_getter = config_getter
        self._store = store or SessionReadStore()
        self._agent: Any = None
        self.stats: dict[str, int] = {}

    def init(self, agent: Any) -> None:
        self._agent = agent

    def uninit(self, agent: Any) -> None:
        self._agent = None

    # ── config ──────────────────────────────────────────────────

    def _config(self) -> Mapping[str, Any]:
        if self._config_getter is None:
            return {}
        try:
            value = self._config_getter()
        except Exception:  # noqa: BLE001
            return {}
        return value if isinstance(value, Mapping) else {}

    def _enabled(self, config: Mapping[str, Any]) -> bool:
        section = ((config.get("modes") or {}).get("code") or {}).get("read_restore")
        return not (isinstance(section, Mapping) and section.get("enabled") is False)

    def _project_dir(self) -> str | None:
        agent = self._agent
        value = getattr(agent, "_jiuwenswarm_code_project_dir", None) or getattr(
            agent, "_jiuwenswarm_project_dir", None
        )
        if not value:
            workspace = getattr(getattr(agent, "deep_config", None), "workspace", None)
            value = getattr(workspace, "root_path", None)
        return str(value) if value else None

    def _count(self, outcome: str) -> None:
        self.stats[outcome] = self.stats.get(outcome, 0) + 1

    # ── recording ───────────────────────────────────────────────

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        name = str(getattr(inputs, "tool_name", "") or "")
        if name not in (_READ_TOOL, _EDIT_TOOL, _WRITE_TOOL):
            return
        data = _result_data(getattr(inputs, "tool_result", None))
        if data is None:
            return
        path = data.get("file_path")
        if not isinstance(path, str) or not path:
            return
        stat_key = _stat_key(path)
        if stat_key is not None:
            _PROCESS_SEEN[path] = stat_key
        config = self._config()
        if not self._enabled(config):
            return
        session_id = _session_id(ctx)
        if not session_id:
            return
        try:
            if name == _READ_TOOL:
                args = _parse_args(getattr(inputs, "tool_args", None))
                try:
                    offset = int(args.get("offset") or 0)
                except (TypeError, ValueError):
                    offset = 0
                try:
                    line_count = int(data.get("line_count") or 0)
                except (TypeError, ValueError):
                    line_count = 0
                record_read(
                    self._store, session_id, path, offset=offset, line_count=line_count
                )
            else:
                kind = KIND_EDIT if name == _EDIT_TOOL else KIND_WRITE
                record_change(self._store, session_id, path, kind)
        except Exception as exc:  # noqa: BLE001 - recording must never break a tool call
            logger.warning("[ReadRestore] record failed for %s: %s", path, exc)

    # ── restoring ───────────────────────────────────────────────

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        name = str(getattr(inputs, "tool_name", "") or "")
        if name not in (_EDIT_TOOL, _WRITE_TOOL):
            return
        try:
            await self._maybe_restore(ctx, name)
        except Exception as exc:  # noqa: BLE001 - never break the edit itself
            logger.warning("[ReadRestore] restore failed: %s", exc)
            self._count("error")

    async def _maybe_restore(self, ctx: AgentCallbackContext, name: str) -> None:
        if _call_skipped(ctx):
            return
        config = self._config()
        if not self._enabled(config):
            return
        args = _parse_args(getattr(ctx.inputs, "tool_args", None))
        path = _resolve_path(args.get("file_path") or args.get("path") or "")
        if not path or not os.path.isfile(path):
            return  # creating a file needs no read
        current_key = _stat_key(path)
        if current_key is not None and _PROCESS_SEEN.get(path) == current_key:
            return  # agent-core already has it in this process
        session_id = _session_id(ctx)
        if not session_id:
            return
        record = self._store.get(session_id, path)
        if record is None:
            self._count("no_record")
            return
        current = fingerprint(path)
        if current is None or not record.matches(current):
            self._count("stale")
            logger.info(
                "[ReadRestore] not restoring %s: changed since session %s saw it",
                path,
                session_id,
            )
            return
        if name == _WRITE_TOOL and not record.complete:
            # agent-core lets write_file overwrite only a fully read file.
            self._count("partial_for_write")
            return
        reason = read_denied_reason(
            path,
            project_dir=self._project_dir(),
            permissions=config.get("permissions")
            if isinstance(config, Mapping)
            else None,
            session_id=session_id,
        )
        if reason:
            self._count("denied")
            logger.info("[ReadRestore] not restoring %s: %s", path, reason)
            return
        tool = self._read_tool(ctx)
        if tool is None:
            self._count("no_tool")
            return
        if name == _EDIT_TOOL:
            ok = await self._read(tool, path, offset=0, limit=1)
        else:
            ok = await self._read_whole(tool, path, current.lines)
        if not ok:
            self._count("read_failed")
            return
        key = _stat_key(path)
        if key is not None:
            _PROCESS_SEEN[path] = key
        self._count("restored")
        logger.info(
            "[ReadRestore] restored read state of %s for %s (session %s saw it earlier)",
            path,
            name,
            session_id,
        )

    def _read_tool(self, ctx: AgentCallbackContext) -> Any:
        """The agent's own registered ``read_file`` tool (same SysOperation)."""
        for agent in (getattr(ctx, "agent", None), self._agent):
            manager = getattr(agent, "ability_manager", None)
            getter = getattr(manager, "get", None)
            if not callable(getter):
                continue
            card = getter(_READ_TOOL)
            tool_id = getattr(card, "id", None)
            if not tool_id:
                continue
            try:
                from openjiuwen.core.runner import Runner

                tool = Runner.resource_mgr.get_tool(tool_id=tool_id)
            except Exception:  # noqa: BLE001
                tool = None
            if tool is not None and callable(getattr(tool, "invoke", None)):
                return tool
        return None

    @staticmethod
    async def _read(tool: Any, path: str, *, offset: int, limit: int) -> bool:
        out = await tool.invoke({"file_path": path, "offset": offset, "limit": limit})
        return getattr(out, "success", False) is True

    async def _read_whole(self, tool: Any, path: str, total_lines: int) -> bool:
        if total_lines <= 0:
            return False
        windows = -(-total_lines // _WRITE_WINDOW_LINES)
        if windows > _MAX_WRITE_WINDOWS:
            return False
        for index in range(windows):
            ok = await self._read(
                tool,
                path,
                offset=index * _WRITE_WINDOW_LINES,
                limit=_WRITE_WINDOW_LINES,
            )
            if not ok:
                return False
        return True


__all__ = ["ReadRestoreRail"]
