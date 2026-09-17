# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team Monitor 处理器.

处理 Team Monitor 的事件流和状态查询，将团队状态转换为前端可消费的格式.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from openjiuwen.agent_teams.monitor import TeamMonitor
from openjiuwen.agent_teams.monitor.models import MonitorEvent, MonitorEventType

from jiuwenswarm.agents.harness.team.event_types import (
    TeamEventCategory,
    resolve_team_event,
)
from jiuwenswarm.agents.harness.team.handlers.base_monitor_handler import BaseMonitorHandler

logger = logging.getLogger(__name__)

# Single server-side convergence point for task status. Each monitor event
# advances the task to a fixed status; the frontend consumes ``status``
# directly and never re-derives it from the event type. Events that carry
# their own status in the payload (created / plan_request / plan_response /
# updated) override the table default via ``event.status or ...``. TASK_UPDATED
# is intentionally absent: its status is purely payload-driven, so a missing
# status must leave the task unchanged rather than reset it.
#
# Adding a new task event is now a one-line entry here plus the SDK mapping in
# ``event_types.py`` — no frontend change required.
_TASK_EVENT_STATUS: dict[MonitorEventType, str] = {
    MonitorEventType.TASK_CREATED: "pending",
    MonitorEventType.TASK_CLAIMED: "in_progress",
    MonitorEventType.TASK_STARTED: "in_progress",
    MonitorEventType.TASK_PLAN_REQUEST: "planning",
    MonitorEventType.TASK_PLAN_RESPONSE: "in_progress",
    MonitorEventType.TASK_COMPLETED: "completed",
    MonitorEventType.TASK_CANCELLED: "cancelled",
    MonitorEventType.TASK_UNBLOCKED: "pending",
    MonitorEventType.TASK_RELEASED: "pending",
    MonitorEventType.TASK_REVOKED: "pending",
    MonitorEventType.TASK_SUBMITTED_FOR_REVIEW: "in_review",
    MonitorEventType.TASK_VERIFIED: "completed",
    MonitorEventType.TASK_REVISION_REQUESTED: "in_progress",
}

# Upper bound (chars) for a task's title/content carried on events / snapshots.
# Titles are short; content holds the full task description, so this is sized
# generously to avoid truncating typical task bodies. Over-limit values get an
# inline marker + structured flags (see ``_truncate_task_text``).
#
# Configurable via the ``JIUWENSWARM_TASK_TEXT_LIMIT`` env var (positive int);
# an unset/blank/invalid/non-positive value falls back to the 4096 default.
_TASK_TEXT_LIMIT_DEFAULT = 4096
_TASK_TEXT_LIMIT_ENV = "JIUWENSWARM_TASK_TEXT_LIMIT"


def _resolve_task_text_limit() -> int:
    """Read the truncation limit from the env, defaulting to 4096.

    A missing, blank, non-integer, or non-positive value falls back to
    ``_TASK_TEXT_LIMIT_DEFAULT`` (with a warning for a malformed value) so a bad
    override never disables truncation or crashes module import.
    """
    raw = os.getenv(_TASK_TEXT_LIMIT_ENV)
    if raw is None or not raw.strip():
        return _TASK_TEXT_LIMIT_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning(
            "[TeamMonitorHandler] 无效的 %s=%r,回退默认 %d",
            _TASK_TEXT_LIMIT_ENV,
            raw,
            _TASK_TEXT_LIMIT_DEFAULT,
        )
        return _TASK_TEXT_LIMIT_DEFAULT
    if value <= 0:
        logger.warning(
            "[TeamMonitorHandler] %s=%d 非正数,回退默认 %d",
            _TASK_TEXT_LIMIT_ENV,
            value,
            _TASK_TEXT_LIMIT_DEFAULT,
        )
        return _TASK_TEXT_LIMIT_DEFAULT
    return value


_TASK_TEXT_LIMIT = _resolve_task_text_limit()
_TASK_BODY_EVENT_TYPES = {MonitorEventType.TASK_CREATED, MonitorEventType.TASK_UPDATED}


def _truncate_task_text(value: str | None) -> tuple[str | None, bool, int]:
    """Truncate a task text field to ``_TASK_TEXT_LIMIT`` with an inline marker.

    Args:
        value: The raw title or content string (may be ``None`` or non-str).

    Returns:
        ``(truncated_str, was_truncated, original_size)``. When ``value`` is not
        a str or empty → ``(value, False, 0)``. When ``len <= _TASK_TEXT_LIMIT``
        → ``(value, False, len)``. Else →
        ``(value[:_TASK_TEXT_LIMIT] + marker, True, len)``.
    """
    if not isinstance(value, str) or not value:
        return value, False, 0
    if len(value) <= _TASK_TEXT_LIMIT:
        return value, False, len(value)
    truncated = value[:_TASK_TEXT_LIMIT] + f"…(truncated, total {len(value)} chars)"
    return truncated, True, len(value)


def _task_text_field(name: str, value: str | None) -> dict[str, Any]:
    """Expand a task text field into a dict with optional truncation flags.

    Emits ``{name: truncated_str}`` plus, ONLY when actually truncated, the
    structured flags ``{name}_truncated: True`` and ``{name}_original_size: int``.
    When not truncated, only the ``{name}`` key is present — zero redundancy in
    the common (under-limit) case. Mirrors the
    ``team_helpers._truncate_team_tool_result_event`` precedent.
    """
    truncated_str, was_truncated, original_size = _truncate_task_text(value)
    field: dict[str, Any] = {name: truncated_str}
    if was_truncated:
        field[f"{name}_truncated"] = True
        field[f"{name}_original_size"] = original_size
    return field


class TeamMonitorHandler(BaseMonitorHandler):
    """Team Monitor 处理器.

    封装 Monitor 的创建、事件处理和状态查询，提供简化的接口给前端.
    """

    def __init__(self, monitor: TeamMonitor, session_id: str):
        super().__init__(monitor, session_id)

    # ------------------------------------------------------------------
    # Collect loop — consumes monitor.events()
    # ------------------------------------------------------------------

    async def _collect_events(self) -> None:
        """后台任务：收集 Monitor 事件."""
        try:
            async for event in self._monitor.events():
                if not self._running:
                    break
                event_dict = await self._convert_event_to_dict(event)
                if event_dict:
                    await self._event_queue.put(event_dict)
        except Exception as e:
            logger.error(
                "[TeamMonitorHandler] 事件收集失败: session_id=%s, error=%s",
                self._session_id,
                e,
            )

    # ------------------------------------------------------------------
    # Event conversion
    # ------------------------------------------------------------------

    async def _handle_member_spawned(self, base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        base["member_id"] = event.member_name
        # 获取成员 role：human_agent → mode="human"，其他保留原 mode
        # 同一次查询顺带带出 display_name：前端一切成员展示都用它，事件不带的话
        # 界面只能退回显示 member_id（内部 slug），观感上像换了个人。
        try:
            member_info = await self._monitor.get_member(event.member_name or "")
            if member_info is not None:
                base["mode"] = "human" if member_info.role == "human_agent" else member_info.role
                base["role"] = member_info.role
                display_name = str(getattr(member_info, "display_name", "") or "").strip()
                if display_name:
                    base["name"] = display_name
                # 外部 CLI 成员（claude / codex）的 role 是 teammate，靠这个字段
                # 才能选对头像。
                cli_agent = str(getattr(member_info, "cli_agent", "") or "").strip()
                if cli_agent:
                    base["cli_agent"] = cli_agent
        except Exception as e:
            logger.warning(
                "[TeamMonitorHandler] 获取成员 role 失败: member=%s, error=%s",
                event.member_name,
                e,
            )
        return base

    @staticmethod
    def _handle_member_status_changed(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        base.update({
            "member_id": event.member_name,
            "old_status": event.old_status,
            "new_status": event.new_status,
        })
        return base

    @staticmethod
    def _handle_member_execution_changed(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        base.update({
            "member_id": event.member_name,
            "old_status": event.old_status,
            "new_status": event.new_status,
        })
        return base

    @staticmethod
    def _handle_member_restarted(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        base.update({
            "member_id": event.member_name,
            "reason": event.reason,
            "restart_count": event.restart_count,
        })
        return base

    @staticmethod
    def _handle_member_shutdown(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        base.update({
            "member_id": event.member_name,
            "force": event.force,
        })
        return base

    async def _lookup_task_body(self, task_id: str) -> tuple[str, str] | None:
        """Re-query the task's title/content via the monitor's public API.

        Args:
            task_id: The DB task id carried by the monitor event.

        Returns:
            ``(title, content)`` when the task exists, else ``None``. On any
            exception (monitor None / query error) logs a warning and returns
            ``None`` so the caller can still emit the event with ``task_id`` +
            ``status`` (no body) — never blocks the event stream.
        """
        if self._monitor is None or not task_id:
            return None
        try:
            task = await self._monitor.get_task(task_id)
        except Exception as e:
            logger.warning(
                "[TeamMonitorHandler] 查 task 体失败: task_id=%s err=%s",
                task_id,
                e,
            )
            return None
        if task is None:
            return None
        return task.title, task.content

    async def _handle_task(self, base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """Converge every task event into the frontend-ready task shape.

        The authoritative task status is resolved once here (server-side) so the
        frontend reads ``status`` directly and never re-derives it from the event
        type. The assignee already rides in ``member_id`` set by the caller.

        For ``TASK_CREATED`` / ``TASK_UPDATED`` only, the title/content are
        re-queried from the DB (via ``_lookup_task_body``) and attached here so
        these events become the single source of truth for the task body — the
        frontend's optimistic card (built from the tool_call ``id``) and the
        event card (built from the DB ``task_id``) can then merge. Body fields
        are truncated to ``_TASK_TEXT_LIMIT`` chars with an inline marker + structured flags
        when over the limit. If the lookup fails (monitor None / exception /
        task not found), the event still carries ``task_id`` + ``status`` (no
        body) and is emitted.

        Args:
            base: Pre-filled event dict (type / team_id / member_id).
            event: Source monitor event.

        Returns:
            The event dict with ``task_id`` and, when known, the resolved ``status``
            and (for CREATED/UPDATED) the ``title`` / ``content`` body.
        """
        base["task_id"] = event.task_id
        resolved = event.status or _TASK_EVENT_STATUS.get(event.event_type)
        if resolved:
            base["status"] = resolved
        if event.event_type in _TASK_BODY_EVENT_TYPES and event.task_id:
            body = await self._lookup_task_body(event.task_id)
            if body is not None:
                title_field = _task_text_field("title", body[0])
                content_field = _task_text_field("content", body[1])
                base.update(title_field)
                base.update(content_field)
        return base

    async def _handle_message(self, base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        message_content, message_protocol = await self._get_message_display(event.message_id)
        base.update({
            "message_id": event.message_id,
            "from_member": event.from_member_name,
            "to_member": event.to_member_name,
            "content": message_content,
            "protocol": message_protocol,
        })
        return base

    async def _handle_broadcast(self, base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        message_content, message_protocol = await self._get_message_display(event.message_id)
        base.update({
            "message_id": event.message_id,
            "from_member": event.from_member_name,
            "content": message_content,
            "protocol": message_protocol,
        })
        return base

    async def _get_message_display(self, message_id: str | None) -> tuple[str, str]:
        if not message_id or not self._monitor:
            return "", "plain"
        try:
            from openjiuwen.agent_teams.context import set_session_id, reset_session_id
            token = set_session_id(self._session_id)
            try:
                messages = await self._monitor.get_messages()
                for message in messages:
                    if message.message_id == message_id:
                        protocol = self._normalize_message_protocol(message.protocol)
                        content = self._normalize_message_content(message.content or "", protocol)
                        return content, protocol
                return "", "plain"
            finally:
                reset_session_id(token)
        except Exception as e:
            logger.warning(
                "[TeamMonitorHandler] 查询消息内容失败: message_id=%s, error=%s",
                message_id,
                e,
            )
            return "", "plain"

    @staticmethod
    def _normalize_message_protocol(protocol: Any) -> str:
        value = str(protocol or "plain").strip().lower()
        return value or "plain"

    @staticmethod
    def _normalize_message_content(content: str, protocol: str) -> str:
        if protocol != "json" or not content.strip():
            return content
        try:
            return json.dumps(json.loads(content), ensure_ascii=False)
        except (TypeError, ValueError):
            return content

    async def _convert_event_to_dict(self, event: MonitorEvent) -> dict[str, Any] | None:
        resolved = resolve_team_event(event.event_type)
        if resolved is None:
            return None
        type_str, event_category = resolved

        event_data: dict[str, Any] = {
            "type": type_str,
            "team_id": event.team_name,
        }

        if event.member_name:
            event_data["member_id"] = event.member_name

        # Task events all converge through a single handler that resolves the
        # authoritative status server-side. Member / message events keep their
        # dedicated handlers because they carry distinct fields.
        if event_category == TeamEventCategory.TASK:
            event_data = await self._handle_task(event_data, event)
        else:
            non_task_handlers = {
                MonitorEventType.MEMBER_SPAWNED: self._handle_member_spawned,
                MonitorEventType.MEMBER_STATUS_CHANGED: self._handle_member_status_changed,
                MonitorEventType.MEMBER_EXECUTION_CHANGED: self._handle_member_execution_changed,
                MonitorEventType.MEMBER_RESTARTED: self._handle_member_restarted,
                MonitorEventType.MEMBER_SHUTDOWN: self._handle_member_shutdown,
                MonitorEventType.MESSAGE: self._handle_message,
                MonitorEventType.BROADCAST: self._handle_broadcast,
            }
            handler = non_task_handlers.get(event.event_type)
            if handler is None:
                return None
            if asyncio.iscoroutinefunction(handler):
                event_data = await handler(event_data, event)
            else:
                event_data = handler(event_data, event)

        return {
            "event_type": event_category.value,
            "session_id": self._session_id,
            "event": event_data,
        }

    # ------------------------------------------------------------------
    # Properties and snapshot
    # ------------------------------------------------------------------

    @property
    def team_id(self) -> str | None:
        return self._monitor.team_name if self._monitor else None

    async def get_team_snapshot(self) -> dict[str, Any] | None:
        """获取当前团队状态快照，用于刷新后恢复成员列表和任务列表。"""
        if self._monitor is None:
            return None
        try:
            members = await self._monitor.get_members()
            team_info = await self._monitor.get_team_info()
            leader_name = team_info.leader_member_name if team_info else None
            if leader_name:
                members = [m for m in members if m.member_name != leader_name]
            tasks = await self._monitor.get_tasks() or []
            return {
                "members": [
                    {
                        "member_id": m.member_name,
                        "name": m.display_name,
                        "status": m.status,
                        "execution_status": m.execution_status,
                        # MemberMode: build_mode/plan_mode（控制是否需要 leader 审批）
                        "mode": m.mode,
                        # role 字段：区分人类/AI（human_agent/teammate/leader）
                        "role": m.role,
                        # 外部 CLI 成员的 role 就是 teammate，只有这个字段能区分
                        # 它是哪种 CLI（claude / codex），前端据此选头像。
                        "cli_agent": getattr(m, "cli_agent", None),
                    }
                    for m in members
                ],
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "team_name": t.team_name,
                        **_task_text_field("title", t.title),
                        **_task_text_field("content", t.content),
                        "status": t.status,
                        "assignee": t.assignee,
                        "updated_at": t.updated_at,
                    }
                    for t in tasks
                ],
                "team_id": self._monitor.team_name,
            }
        except Exception as e:
            logger.warning(
                "[TeamMonitorHandler] get_team_snapshot failed: session_id=%s, error=%s",
                self._session_id,
                e,
            )
            return None

    async def get_member_list(self) -> list[dict[str, Any]] | None:
        """仅查询成员列表（不含 tasks）。

        ``get_team_snapshot`` 把 members 与 tasks 绑在同一个 try 里，一旦
        ``get_tasks()`` 抛错（如 team 任务表尚未建表/迁移，``no such table``），
        整个 snapshot 返回 None，连 members 一起丢失。/join 成员校验只需要
        members，不依赖 tasks，故提供此窄方法做降级：tasks 取不到不影响
        成员名校验。字段形状与 ``get_team_snapshot`` 的 members 项保持一致。
        """
        if self._monitor is None:
            return None
        try:
            members = await self._monitor.get_members()
            team_info = await self._monitor.get_team_info()
            leader_name = team_info.leader_member_name if team_info else None
            if leader_name:
                members = [m for m in members if m.member_name != leader_name]
            return [
                {
                    "member_id": m.member_name,
                    "name": m.display_name,
                    "status": m.status,
                    "execution_status": m.execution_status,
                    "mode": m.mode,
                    "role": m.role,
                    "cli_agent": getattr(m, "cli_agent", None),
                }
                for m in members
            ]
        except Exception as e:
            logger.warning(
                "[TeamMonitorHandler] get_member_list failed: session_id=%s, error=%s",
                self._session_id,
                e,
            )
            return None

    @staticmethod
    async def get_member_list_from_db(
        team_name: str,
        *,
        exclude_leader: bool = False,
    ) -> list[dict[str, Any]] | None:
        """monitor 不在时直查 ``team.db`` 取该 team 的全部成员（/join 成员校验降级）。

        monitor 是 runtime 运行态对象，runtime 被 chat.interrupt 中断 / 后端重启
        / 轮间 idle 时 monitor 不在，但成员定义静态持久于全局 ``team_member`` 表，
        不依赖 monitor 存活。本方法在 monitor 不可达时供 server 层降级调用，
        使 /join 成员校验通过 runtime 间隙继续工作。

        只查全局 ``team_member`` 表，默认返回该 team 的**全部**成员（不按 role 过滤、
        不排除 leader）——业务过滤（``role == "human_agent"`` 等）由调用方做，
        与 ``get_member_list`` 的职责边界一致。member dict 形状（member_id/name/
        status/execution_status/mode/role）与 ``get_member_list`` 保持一致，便于
        调用方统一过滤。

        ``initialize()`` 幂等：runtime 已初始化过单例则 no-op；首次由本路径初始化
        时只建全局表（``create_cur_session_tables`` 无 session 上下文会跳过动态表，
        但本方法只查全局表，不受影响）。异常或 db 不可达返回 None，调用方按
        "未就绪"语义处理。

        Args:
            team_name: 要查询的 team 名。
            exclude_leader: 是否剔除 leader 行，与 ``get_member_list`` 对齐。判据只
                能是 team 行上的 ``leader_member_name``——leader 的成员行由
                ``build_team`` 经默认 role 建出，``role`` 字段和普通 teammate 一样，
                按 role 过滤筛不掉它。
        """
        from openjiuwen.agent_teams.spawn.shared_resources import get_shared_db
        from openjiuwen.agent_teams.tools.database.config import DatabaseConfig
        from openjiuwen.agent_teams.tools.member_options import load_member_options

        from jiuwenswarm.common.config import get_config
        from jiuwenswarm.agents.harness.team.config_loader import resolve_team_sqlite_db_path

        if not team_name:
            return None
        db_path = resolve_team_sqlite_db_path(get_config())
        if db_path is None:
            return None
        db = get_shared_db(DatabaseConfig(db_type="sqlite", connection_string=str(db_path)))

        # db.member dao 在 initialize() 后才挂载（未初始化时为 None），故必须先
        # initialize；幂等，runtime 已起过则 no-op。
        await db.initialize()
        members = await db.member.get_team_members(team_name, status=None) or []

        if exclude_leader:
            team_info = await db.team.get_team(team_name)
            leader_name = str(getattr(team_info, "leader_member_name", "") or "").strip()
            if leader_name:
                members = [m for m in members if m.member_name != leader_name]

        return [
            {
                "member_id": m.member_name,
                "name": m.display_name,
                "status": m.status,
                "execution_status": m.execution_status,
                "mode": m.mode,
                "role": m.role,
                # 直查 DB 拿到的是原始行，cli_agent 埋在 options JSON 里；
                # monitor 路径的 MemberInfo 已经把它解出来了，这里对齐字段形状。
                "cli_agent": load_member_options(getattr(m, "options", None)).cli_agent,
            }
            for m in members
        ]

    @staticmethod
    async def get_team_snapshot_from_db(
        session_id: str,
        team_name: str,
    ) -> dict[str, Any] | None:
        """monitor 不在时直查 ``team.db`` 拼出与 ``get_team_snapshot`` 同形的快照.

        历史恢复走 ``team.snapshot`` RPC 时，runtime / monitor 往往已停，但
        任务行仍在 per-session 动态表 ``team_task_<hash>`` 里。本方法绑定
        ``session_id`` 上下文后直查 DB，使历史面板在无 monitor 时仍能拿到
        带 title/content（经统一截断）的真源任务卡。

        Args:
            session_id: 会话 id，用于解析动态 task 表后缀。
            team_name: 持久化在 session metadata 中的 team 名。

        Returns:
            ``{members, tasks, team_id}`` 或 None（参数/路径无效、查询失败）。
        """
        from openjiuwen.agent_teams.context import reset_session_id, set_session_id
        from openjiuwen.agent_teams.spawn.shared_resources import get_shared_db
        from openjiuwen.agent_teams.tools.database.config import DatabaseConfig
        from openjiuwen.agent_teams.tools.member_options import load_member_options

        from jiuwenswarm.agents.harness.team.config_loader import resolve_team_sqlite_db_path
        from jiuwenswarm.common.config import get_config

        sid = str(session_id or "").strip()
        tname = str(team_name or "").strip()
        if not sid or not tname:
            return None
        db_path = resolve_team_sqlite_db_path(get_config())
        if db_path is None:
            return None

        db = get_shared_db(DatabaseConfig(db_type="sqlite", connection_string=str(db_path)))
        token = set_session_id(sid)
        try:
            await db.initialize()
            # Ensure the per-session dynamic task table is mapped (checkfirst);
            # historical sessions already have the table from the live run.
            await db.create_cur_session_tables()

            team_info = await db.team.get_team(tname)
            leader_name = (
                str(getattr(team_info, "leader_member_name", "") or "").strip()
                if team_info is not None
                else ""
            )
            members = await db.member.get_team_members(tname, status=None) or []
            if leader_name:
                members = [m for m in members if m.member_name != leader_name]
            tasks = await db.task.get_team_tasks(tname) or []

            return {
                "members": [
                    {
                        "member_id": m.member_name,
                        "name": m.display_name,
                        "status": m.status,
                        "execution_status": m.execution_status,
                        "mode": m.mode,
                        "role": m.role,
                        # 外部 CLI 成员的 role 就是 teammate，只有这个字段能区分
                        # 它是哪种 CLI（claude / codex），前端据此选头像。
                        "cli_agent": load_member_options(getattr(m, "options", None)).cli_agent,
                    }
                    for m in members
                ],
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "team_name": t.team_name,
                        **_task_text_field("title", t.title),
                        **_task_text_field("content", t.content),
                        "status": t.status,
                        "assignee": t.assignee,
                        "updated_at": t.updated_at,
                    }
                    for t in tasks
                ],
                "team_id": tname,
            }
        except Exception as e:
            logger.warning(
                "[TeamMonitorHandler] get_team_snapshot_from_db failed: "
                "session_id=%s team_name=%s error=%s",
                sid,
                tname,
                e,
            )
            return None
        finally:
            reset_session_id(token)
