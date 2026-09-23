# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional


from jiuwenswarm.common.config import (
    get_config,
)
from jiuwenswarm.common.config_panel import tui_models_handlers
from jiuwenswarm.gateway.routing.route_binding import GatewayRouteBinding
from jiuwenswarm.gateway.routing.agent_request_timeout import (
    resolve_agent_request_timeout_seconds,
    send_agent_request_with_timeout,
)

logger = logging.getLogger(__name__)

_HARMONYOS_DEV_INIT_TASKS_ATTR = "_jiuwenswarm_harmonyos_dev_init_tasks"
_TUI_EXPLICIT_EXIT_CANCEL_GRACE_SECONDS = 1.0


def _get_harmonyos_dev_init_tasks(
    ws: Any, *, create: bool
) -> set[asyncio.Task[Any]] | None:
    """Return the Dev Init tasks owned by a websocket."""
    tasks = getattr(ws, _HARMONYOS_DEV_INIT_TASKS_ATTR, None)
    if isinstance(tasks, set):
        return tasks
    if not create:
        return None
    tasks = set()
    try:
        setattr(ws, _HARMONYOS_DEV_INIT_TASKS_ATTR, tasks)
    except Exception:
        logger.debug(
            "[harmonyos.dev_init] websocket does not support task tracking",
            exc_info=True,
        )
        return None
    return tasks


async def _cancel_harmonyos_dev_init_tasks(ws: Any) -> None:
    """Cancel and drain Dev Init tasks owned by a websocket."""
    tracked = _get_harmonyos_dev_init_tasks(ws, create=False)
    if tracked is None:
        return
    tasks = [task for task in tracked if isinstance(task, asyncio.Task)]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    tracked.clear()


def _resolve_agent_client(agent_client: Any) -> Any:
    if isinstance(agent_client, dict):
        return agent_client.get("value")
    return agent_client


async def _send_tui_agent_request(real_client: Any, env: Any, *, label: str) -> Any:
    timeout_seconds = resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method=getattr(env, "method", None),
        is_stream=bool(getattr(env, "is_stream", False)),
    )
    return await send_agent_request_with_timeout(
        real_client,
        env,
        label=f"tui {label}",
        timeout_seconds=timeout_seconds,
    )


async def _tui_config_send_request(client: Any, env: Any, *, label: str) -> Any:
    """config 域下沉 handler 的请求发送器。

    延迟解析 ``_send_tui_agent_request``（调用时查模块全局），保留单测对该
    函数的 monkeypatch 语义；下沉模块未注入时退回 ``client.send_request`` 直发。
    """
    return await _send_tui_agent_request(client, env, label=label)


# ── 需要转发到 Agent 的方法集合 ──────────────────────────────

CLI_FORWARD_REQ_METHODS = frozenset(
    {
        "command.add_dir",
        "command.btw",
        "command.chrome",
        "command.compact",
        "command.compact_partial",
        "command.context",
        "command.recap",
        "command.diff",
        "command.simplify",
        "command.mcp",
        "command.resume",
        "command.sandbox",
        "command.session",
        "command.workflows",
        "swarmflow.pause",
        "swarmflow.resume",
        "swarmflow.stop",
        "command.status",
        "command.goal",
        "chat.send",
        "chat.interrupt",
        "chat.resume",
        "chat.user_answer",
        "chat.swarmflow_reply",
        "history.get",
        "skills.marketplace.list",
        "skills.list",
        "skills.installed",
        "skills.get",
        "skills.toggle",
        "skills.install",
        "skills.import_local",
        "skills.import_upload",
        "skills.create_from_knowledge",
        "skills.marketplace.add",
        "skills.marketplace.remove",
        "skills.marketplace.toggle",
        "skills.uninstall",
        "skills.online_search.search",
        "skills.online_search.install",
        "skills.skillnet.search",
        "skills.skillnet.install",
        "skills.skillnet.install_status",
        "skills.skillnet.evaluate",
        "skills.clawhub.get_token",
        "skills.clawhub.set_token",
        "skills.clawhub.search",
        "skills.clawhub.download",
        "skills.teamskillshub.info",
        "skills.teamskillshub.init",
        "skills.teamskillshub.validate",
        "skills.teamskillshub.pack",
        "skills.teamskillshub.search",
        "skills.swarmskillshub.recommend",
        "skills.teamskillshub.install",
        "skills.teamskillshub.publish",
        "skills.teamskillshub.delete",
        "skills.swarmskillshub.detail",
        "skills.evolution.status",
        "skills.evolution.get",
        "skills.evolution.save",
        "plugins.list",
        "plugins.install",
        "plugins.uninstall",
        "plugins.enable",
        "plugins.disable",
        "plugins.reload",
        "agent_groups.list",
        "agent_groups.show",
        "agent_groups.file.list",
        "agent_groups.file.read",
        "agent_groups.create",
        "agent_groups.import_local",
        "agent_groups.install",
        "agent_groups.uninstall",
        "agent_templates.list",
        "agent_templates.show",
        "agent_templates.file.list",
        "agent_templates.file.read",
        "agent_templates.create",
        "agent_templates.update",
        "agent_templates.delete",
        "agent_templates.import_local",
        "agent_templates.install",
        "agent_templates.uninstall",
        "plugin_packages.list",
        "plugin_packages.show",
        "plugin_packages.create",
        "plugin_packages.import_local",
        "plugin_packages.install",
        "plugin_packages.uninstall",
        "permissions.tools.get",
        "permissions.tools.update",
        "permissions.tools.delete",
        "permissions.rules.get",
        "permissions.rules.create",
        "permissions.rules.update",
        "permissions.rules.delete",
        "extensions.list",
        "extensions.import",
        "extensions.delete",
        "extensions.toggle",
        "session.switch",
        "team.templates.list",
        "team.bindings.list",
        "team.binding.create",
        "team.binding.generate",
        "team.session.bind",
        "team.mq.publish",
        "session.fork",
        # Agent configuration
        "agents.list",
        "agents.get",
        "agents.create",
        "agents.update",
        "agents.delete",
        "agents.enable",
        "agents.disable",
        "agents.tools_list",
        # Schedule task management
        "schedule.check_config",
        "schedule.update_config",
        "schedule.create",
        "schedule.run",
        "schedule.list",
        "schedule.status",
        "schedule.logs",
        "schedule.cancel",
        "schedule.delete",
        "issue.watch_once",
        "issue.state.list",
        "issue.matrix",
        "issue.delete",
    }
)

CLI_FORWARD_NO_LOCAL_HANDLER_METHODS = frozenset(
    {
        "command.add_dir",
        "command.btw",
        "command.chrome",
        "command.compact",
        "command.compact_partial",
        "command.context",
        "command.recap",
        "command.diff",
        "command.simplify",
        "command.mcp",
        "command.resume",
        "command.sandbox",
        "command.session",
        "command.workflows",
        "swarmflow.pause",
        "swarmflow.resume",
        "swarmflow.stop",
        "command.status",
        "command.goal",
        "skills.marketplace.list",
        "skills.list",
        "skills.installed",
        "skills.get",
        "skills.toggle",
        "skills.install",
        "skills.import_local",
        "skills.import_upload",
        "skills.create_from_knowledge",
        "skills.marketplace.add",
        "skills.marketplace.remove",
        "skills.marketplace.toggle",
        "skills.uninstall",
        "skills.online_search.search",
        "skills.online_search.install",
        "skills.skillnet.search",
        "skills.skillnet.install",
        "skills.skillnet.install_status",
        "skills.skillnet.evaluate",
        "skills.clawhub.get_token",
        "skills.clawhub.set_token",
        "skills.clawhub.search",
        "skills.clawhub.download",
        "skills.teamskillshub.info",
        "skills.teamskillshub.init",
        "skills.teamskillshub.validate",
        "skills.teamskillshub.pack",
        "skills.teamskillshub.search",
        "skills.swarmskillshub.recommend",
        "skills.teamskillshub.install",
        "skills.teamskillshub.publish",
        "skills.teamskillshub.delete",
        "skills.swarmskillshub.detail",
        "skills.evolution.status",
        "skills.evolution.get",
        "skills.evolution.save",
        "plugins.list",
        "plugins.install",
        "plugins.uninstall",
        "plugins.enable",
        "plugins.disable",
        "plugins.reload",
        "agent_groups.list",
        "agent_groups.show",
        "agent_groups.file.list",
        "agent_groups.file.read",
        "agent_groups.create",
        "agent_groups.import_local",
        "agent_groups.install",
        "agent_groups.uninstall",
        "agent_templates.list",
        "agent_templates.show",
        "agent_templates.file.list",
        "agent_templates.file.read",
        "agent_templates.create",
        "agent_templates.update",
        "agent_templates.delete",
        "agent_templates.import_local",
        "agent_templates.install",
        "agent_templates.uninstall",
        "plugin_packages.list",
        "plugin_packages.show",
        "plugin_packages.create",
        "plugin_packages.import_local",
        "plugin_packages.install",
        "plugin_packages.uninstall",
        "permissions.tools.get",
        "permissions.tools.update",
        "permissions.tools.delete",
        "permissions.rules.get",
        "permissions.rules.create",
        "permissions.rules.update",
        "permissions.rules.delete",
        "extensions.list",
        "extensions.import",
        "extensions.delete",
        "extensions.toggle",
        "session.switch",
        "team.templates.list",
        "team.bindings.list",
        "team.binding.create",
        "team.binding.generate",
        "team.session.bind",
        "team.mq.publish",
        "session.fork",
        # Agent configuration
        "agents.list",
        "agents.get",
        "agents.create",
        "agents.update",
        "agents.delete",
        "agents.enable",
        "agents.disable",
        "agents.tools_list",
        # Schedule task management
        "schedule.check_config",
        "schedule.update_config",
        "schedule.create",
        "schedule.run",
        "schedule.list",
        "schedule.status",
        "schedule.logs",
        "schedule.cancel",
        "schedule.delete",
        "issue.watch_once",
        "issue.state.list",
        "issue.matrix",
        "issue.delete",
    }
)


@dataclass
class CliHandlersBindParams:
    channel: Any  # GatewayServer instance
    agent_client: Any = None
    message_handler: Any = None
    third_agent: Any = None
    on_config_saved: Any = None
    path: str = "/tui"
    cron_controller: Any = None
    heartbeat_controller: Any = None
    # AgentServer ConfigAdapter reuses the mature TUI command implementation
    # inside the target user directory.  It must not proxy command.model back
    # to Gateway a second time.
    force_local_config: bool = False


@dataclass
class CliRouteBindParams:
    agent_client: Any = None
    message_handler: Any = None
    third_agent: Any = None
    on_config_saved: Any = None
    path: str = "/tui"
    channel_id: str = "tui"
    cron_controller: Any = None
    # 新 Heartbeat(线程续跑)controller,TUI 通道支持 heartbeat.job.* 调用。
    heartbeat_controller: Any = None
    # V2: 委托 ws 注册的 TuiChannel 实例。GatewayServer 仍作 /tui ws 宿主 + 入站帧解析，
    # 但把 ws + RoutingKey 委托注册进 TuiChannel 的五维索引，出站由 ChannelManager
    # 派发到 TuiChannel.send（按 delivery.ws_id 物理寻址）。
    ws_channel: Any = None


@dataclass
class ForwardRewindE2AParams:
    """Parameters for forwarding rewind request to AgentServer via E2A."""

    ws: Any
    req_id: str
    target_sid: str
    turn_index: int
    req_method: Any
    error_label: str
    user_id: str | None = None


def resolve_tui_session_project_path(session: dict[str, Any] | None) -> str:
    """解析 TUI session 的项目路径，供 /resume current-dir 过滤与展示。

    优先 ``channel_metadata.project_dir`` / ``cwd``（与历史 chat 落盘一致），
    回退顶层 ``project_dir``（``session.create`` / ``/clear`` 写入）。
    修复 Issue #2503：创建后尚未发聊时 channel_metadata 为空导致 current-dir 漏列。
    """
    if not isinstance(session, dict):
        return ""
    ch_meta = session.get("channel_metadata")
    if isinstance(ch_meta, dict):
        for key in ("project_dir", "cwd"):
            raw = ch_meta.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    top = session.get("project_dir")
    if isinstance(top, str) and top.strip():
        return top.strip()
    return ""


def tui_session_matches_project_dir(
    session: dict[str, Any] | None,
    project_dir: str,
    *,
    show_all_projects: bool = False,
) -> bool:
    """判断 session 是否属于 ``project_dir``（current-dir resume 过滤）。"""
    if show_all_projects:
        return True
    project_dir = str(project_dir or "").strip()
    if not project_dir:
        return True
    session_project = resolve_tui_session_project_path(session)
    if not session_project:
        return False
    try:
        project_dir = os.path.realpath(project_dir)
    except OSError:
        pass
    try:
        session_project = os.path.realpath(session_project)
    except OSError:
        pass
    session_norm = os.path.normcase(os.path.normpath(session_project))
    project_norm = os.path.normcase(os.path.normpath(project_dir))
    if session_norm == project_norm:
        return True
    return session_norm.startswith(project_norm + os.sep)


def build_tui_session_create_channel_metadata(
    params: dict[str, Any] | None,
    resolved_project_dir: str = "",
) -> dict[str, Any] | None:
    """为 TUI ``session.create`` 构造应同步落盘的 ``channel_metadata``。

    路径优先用项目预解析结果，否则回退请求中的 ``project_dir`` / ``cwd``。
    """
    seed = str(resolved_project_dir or "").strip()
    if not seed and isinstance(params, dict):
        for key in ("project_dir", "cwd"):
            raw = params.get(key)
            if isinstance(raw, str) and raw.strip():
                seed = raw.strip()
                break
    if not seed:
        return None
    meta: dict[str, Any] = {"project_dir": seed, "cwd": seed}
    try:
        from jiuwenswarm.common.utils import resolve_git_branch

        meta["git_branch"] = resolve_git_branch(seed)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[TUI] session.create resolve_git_branch failed for %s", seed, exc_info=True
        )
    return meta


def resolve_3rdagent_switch_session_id(params: dict | None) -> str:
    """Explicit ``params.session_id`` for 3rdagent.switch (never gateway req_id fallback)."""
    if not isinstance(params, dict):
        return ""
    return str(params.get("session_id") or "").strip()


def register_cli_handlers(bind: CliHandlersBindParams) -> None:
    channel = bind.channel
    agent_client = bind.agent_client
    on_config_saved = bind.on_config_saved
    path = bind.path
    cron_controller_ref = bind.cron_controller
    heartbeat_controller_ref = bind.heartbeat_controller
    force_local_config = bind.force_local_config
    from jiuwenswarm.gateway.routing.third_agent import get_unsupported_third_agent

    third_agent = bind.third_agent if bind.third_agent is not None else get_unsupported_third_agent()
    harmonyos_dev_init_tasks: dict[tuple[int, str], asyncio.Task[Any]] = {}

    async def _config_get(ws, req_id, params, session_id):
        """config.get 实现已下沉 ``common.config_panel.tui_models_handlers``。"""
        await tui_models_handlers.config_get_handler(
            channel, ws, req_id, params, session_id
        )

    async def _config_set(ws, req_id, params, session_id):
        """config.set 实现已下沉 ``common.config_panel.tui_models_handlers``。"""
        await tui_models_handlers.config_set_handler(
            channel, ws, req_id, params, session_id,
            on_config_saved=on_config_saved,
            agent_client=agent_client,
            send_request=_tui_config_send_request,
        )

    async def _config_validate_model(ws, req_id, params, session_id):
        """config.validate_model 实现已下沉（TUI 契约与 Web 侧分模块保留）。"""
        await tui_models_handlers.config_validate_model_handler(
            channel, ws, req_id, params
        )

    async def _session_list(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary

        limit = 20
        if isinstance(params, dict):
            raw_limit = params.get("limit")
            if isinstance(raw_limit, int):
                limit = raw_limit
            elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
                limit = int(raw_limit.strip())
        limit = max(1, min(limit, 200))

        real_client = _resolve_agent_client(agent_client)
        # Preserve the pre-refactor single-user startup/offline behavior: the
        # TUI can render an empty history before its AgentServer client exists.
        if real_client is None:
            await channel.send_response(ws, req_id, ok=True, payload={"sessions": []})
            return
        ok, payload = await fetch_agent_unary(
            agent_client=real_client,
            req_method=ReqMethod.SESSION_LIST,
            params=params or {},
            user_id=user_id,
            session_id=session_id,
            channel_id="tui",
            label="session.list",
        )
        if not ok:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(payload.get("error") or "session.list failed"),
                code=str(payload.get("code") or "SERVICE_UNAVAILABLE"),
            )
            return
        all_sessions = (
            payload.get("sessions", [])
            if isinstance(payload, dict)
            else []
        )
        # 过滤掉 None/非 dict/无效 session_id，防止前端 SelectList.render() 崩溃
        normalized_sessions = []
        for s in all_sessions:
            if not s or not isinstance(s, dict):
                continue
            raw_sid = s.get("session_id")
            if isinstance(raw_sid, str):
                normalized_session_id = raw_sid.strip()
            elif raw_sid is not None:
                normalized_session_id = str(raw_sid).strip()
            else:
                normalized_session_id = ""
            if not normalized_session_id:
                continue
            s["session_id"] = normalized_session_id
            normalized_sessions.append(s)
        all_sessions = normalized_sessions
        # 按项目目录过滤 + 排除当前会话（对齐 /resume 行为）
        # all_projects=True 时跳过项目过滤，列出所有项目的会话（Ctrl+A）
        show_all_projects = (
            bool(params.get("all_projects"))
            if isinstance(params, dict) else False
        )
        project_dir = (
            str(params.get("project_dir", "")).strip()
            if isinstance(params, dict) else ""
        )
        # 规范化路径以处理 macOS 符号链接（如 /tmp → /private/tmp）
        if project_dir:
            try:
                project_dir = os.path.realpath(project_dir)
            except OSError:
                pass
        current_sid = str(session_id or "").strip()

        cli_sessions = []
        for s in all_sessions:
            if s.get("channel_id", "") != "tui":
                continue
            if not tui_session_matches_project_dir(
                s, project_dir, show_all_projects=show_all_projects
            ):
                continue
            if s.get("session_id", "") == current_sid:
                continue
            cli_sessions.append(s)
        # 按 last_message_at 降序排序（最近活跃优先）
        cli_sessions.sort(
            key=lambda s: s.get("last_message_at", 0) or 0, reverse=True
        )
        cli_sessions = cli_sessions[:limit]

        # 附带每个会话的 project_dir / git_branch 供前端判断跨项目恢复 + 按分支过滤
        for s in cli_sessions:
            ch_meta = s.get("channel_metadata") if isinstance(s.get("channel_metadata"), dict) else {}
            sp = resolve_tui_session_project_path(s)
            if sp:
                try:
                    sp = os.path.realpath(sp)
                except OSError:
                    pass
            s["project_dir"] = sp
            # 会话首条消息时记录的分支；存量会话无该字段时回填空串（前端按"兜底显示"处理）
            s["git_branch"] = str(ch_meta.get("git_branch") or "").strip()

        # 标记已在其他 TUI 窗口中打开的会话，供前端拦截冲突的 /resume
        try:
            active_session_ids = channel.get_active_session_ids("tui", exclude_ws=ws)
        except Exception:
            logger.warning(
                "[tui] session.list: get_active_session_ids failed, active_in_window degraded",
                exc_info=True,
            )
            active_session_ids = set()
        for s in cli_sessions:
            if s.get("session_id") in active_session_ids:
                s["active_in_window"] = True

        # 当前项目的 git 分支，供前端 Ctrl+B 过滤对比（非 git/失败为哨兵 "HEAD"）
        from jiuwenswarm.common.utils import resolve_git_branch

        current_branch = resolve_git_branch(project_dir or None)

        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={"sessions": cli_sessions, "current_branch": current_branch},
        )

    async def _session_create(ws, req_id, params, session_id, user_id=None):
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        create_params = dict(params)
        requested_session_id = str(create_params.get("session_id") or "").strip()
        if requested_session_id:
            # TUI --session compatibility: preserve the supplied ID and let
            # AgentServer resolve/persist its authoritative project binding.
            create_params["session_id"] = requested_session_id
        else:
            create_params.pop("session_id", None)
            # Phase 3: the cwd → code project binding is resolved by the target
            # AgentServer in its injected user directory; the Gateway only mints
            # the prewarm claim token and forwards the raw params.
            create_params.setdefault("create_token", secrets.token_hex(16))
        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve_agent_client(agent_client),
            ws=ws,
            req_id=req_id,
            params=create_params,
            session_id=None,
            user_id=user_id or getattr(ws, "_gateway_user_id", None),
            req_method=ReqMethod.SESSION_CREATE,
            label="session.create",
            default_error_code="SESSION_CREATE_FAILED",
        )

    async def _session_rebind_project(ws, req_id, params, session_id, user_id=None):
        """TUI 专用：``/workspace set`` 切换工作目录时同步重绑当前 session 的 project。

        会话运行态与 metadata 写入权由 AgentServer 持有，始终经 E2A 转发到
        AgentServer 的 ``session.rebind_project`` handler（分离部署 / user_id 隔离
        目录时，Gateway 本地写不会落到正确会话目录）。AgentOS 下 AgentServer
        不可达时返回可重试错误；单用户 WebSocket 客户端与 AgentServer 共享目录，
        恢复迁移前的本地重绑路径以保持离线可用性。
        """
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
        )

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target = str(params.get("session_id") or session_id or "").strip()
        if not target:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        candidate_dir = str(params.get("project_dir") or "").strip()
        if not candidate_dir:
            await channel.send_response(
                ws, req_id, ok=False, error="project_dir is required", code="BAD_REQUEST"
            )
            return

        forward_params = {
            "session_id": target,
            "project_dir": candidate_dir,
            **{k: v for k, v in params.items() if k not in ("session_id", "project_dir")},
        }
        real_client = _resolve_agent_client(agent_client)
        # A remote non-AgentOS extension (for example YuanRong) also has an
        # isolated user directory.  Only the stock local WebSocket client may
        # use the historical shared-directory fallback.
        legacy_shared_dir = is_legacy_shared_directory_client(real_client)

        async def _rebind_from_shared_dir() -> None:
            """Pre-AgentOS behavior, valid only when both processes share one dir."""
            from jiuwenswarm.server.runtime.session.project_store import (
                find_or_create_code_project_for_tui_params,
            )
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
                rebind_session_project,
            )

            if not get_session_metadata(target):
                await channel.send_response(
                    ws, req_id, ok=False, error="session not found", code="NOT_FOUND"
                )
                return
            try:
                project = find_or_create_code_project_for_tui_params(
                    {"project_dir": candidate_dir}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[tui] session.rebind_project: resolve project failed: %s", exc
                )
                await channel.send_response(
                    ws, req_id, ok=False, error=str(exc), code="PROJECT_RESOLVE_FAILED"
                )
                return
            if project is None:
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="project_dir must be a non-empty absolute path",
                    code="BAD_REQUEST",
                )
                return
            updated = rebind_session_project(
                session_id=target,
                project_id=project.project_id,
                project_dir=project.project_dir,
                work_mode=project.work_mode,
            )
            if not updated:
                await channel.send_response(
                    ws, req_id, ok=False, error="session not found", code="NOT_FOUND"
                )
                return
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={
                    "session_id": target,
                    "project_id": project.project_id,
                    "project_dir": project.project_dir,
                    "project_name": project.name,
                    "work_mode": project.work_mode,
                },
            )

        # 与 e2a_proxy 的判定语义对齐：兼容/扩展 client 可能不暴露
        # ``server_ready``，缺省视为可达，只有显式 False 才视为不可达。
        if real_client is None or getattr(real_client, "server_ready", True) is False:
            if legacy_shared_dir:
                await _rebind_from_shared_dir()
                return
            await channel.send_response(
                ws, req_id, ok=False,
                error="AgentServer is unavailable", code="SERVICE_UNAVAILABLE",
            )
            return
        try:
            env = e2a_from_agent_fields(
                request_id=req_id,
                channel_id="tui",
                session_id=target,
                req_method=ReqMethod.SESSION_REBIND_PROJECT,
                params=forward_params,
                is_stream=False,
                timestamp=time.time(),
                user_id=user_id or getattr(ws, "_gateway_user_id", None),
            )
            resp = await _send_tui_agent_request(
                real_client, env, label="session.rebind_project",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[tui] session.rebind_project forward to agent failed: %s", exc
            )
            if legacy_shared_dir:
                await _rebind_from_shared_dir()
                return
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="SERVICE_UNAVAILABLE",
            )
            return
        pl = resp.payload if isinstance(resp.payload, dict) else {}
        if resp.ok:
            await channel.send_response(ws, req_id, ok=True, payload=pl)
            return
        err = pl.get("error", "session.rebind_project failed")
        code = pl.get("code") or None
        if isinstance(code, str) and not code.strip():
            code = None
        await channel.send_response(
            ws, req_id, ok=False, error=str(err), code=code,
        )


    async def _session_delete(ws, req_id, params, session_id, user_id=None):
        """删除一个 session（统一薄代理 E2A 转发）。

        AgentServer 不可达时返回 SERVICE_UNAVAILABLE；Gateway 不再跑本地
        SessionAdapter 删除路径。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target = str(params.get("session_id") or "").strip()
        if not target:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve_agent_client(agent_client),
            ws=ws,
            req_id=req_id,
            params=params,
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_DELETE,
            label="session.delete",
        )

    async def _forward_rewind_e2a(params: ForwardRewindE2AParams) -> bool:
        """Try to forward a rewind request to AgentServer via E2A.

        Returns True if the request was successfully handled by AgentServer.
        In AgentOS, failures are returned to the client and never fall back to
        the Gateway deployment directory.  The legacy local fallback remains
        available only for the single-user WebSocket client.
        """
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
        )

        real_client = _resolve_agent_client(agent_client)
        if real_client is None:
            await channel.send_response(
                params.ws, params.req_id, ok=False,
                error="AgentServer is unavailable", code="SERVICE_UNAVAILABLE",
            )
            return True

        try:
            env = e2a_from_agent_fields(
                request_id=params.req_id,
                channel_id="tui",
                session_id=params.target_sid,
                req_method=params.req_method,
                params={"session_id": params.target_sid, "turn_index": params.turn_index},
                is_stream=False,
                timestamp=time.time(),
                user_id=params.user_id,
            )
            resp = await _send_tui_agent_request(
                real_client, env, label=params.error_label,
            )
            if resp.ok:
                pl = resp.payload if isinstance(resp.payload, dict) else {}
                await channel.send_response(params.ws, params.req_id, ok=True, payload=pl)
                return True
            pl = resp.payload if isinstance(resp.payload, dict) else {}
            err = pl.get("error", params.error_label)
            if not is_legacy_shared_directory_client(real_client):
                await channel.send_response(
                    params.ws, params.req_id, ok=False, error=str(err),
                    code=pl.get("code") or "BAD_REQUEST",
                )
                return True
            logger.warning("[cli %s] AgentServer returned error, fallback local: %s", params.error_label, err)
            return False
        except Exception as e:
            if not is_legacy_shared_directory_client(real_client):
                await channel.send_response(
                    params.ws, params.req_id, ok=False, error=str(e),
                    code="SERVICE_UNAVAILABLE",
                )
                return True
            logger.warning("[cli %s] forward to agent failed, fallback local: %s", params.error_label, e)
            return False

    async def _forward_tui_unary(
        ws, req_id, params, session_id, user_id, *, req_method, label
    ) -> bool:
        """Forward a TUI user-state operation; only legacy mode may fall back."""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
        )

        real_client = _resolve_agent_client(agent_client)
        if real_client is None:
            await channel.send_response(
                ws, req_id, ok=False, error="AgentServer is unavailable",
                code="SERVICE_UNAVAILABLE",
            )
            return True
        try:
            env = e2a_from_agent_fields(
                request_id=req_id,
                channel_id="tui",
                session_id=session_id,
                req_method=req_method,
                params=params if isinstance(params, dict) else {},
                is_stream=False,
                timestamp=time.time(),
                user_id=user_id,
            )
            response = await _send_tui_agent_request(real_client, env, label=label)
        except Exception as exc:  # noqa: BLE001
            if not is_legacy_shared_directory_client(real_client):
                await channel.send_response(
                    ws, req_id, ok=False, error=str(exc), code="SERVICE_UNAVAILABLE"
                )
                return True
            return False
        payload = response.payload if isinstance(response.payload, dict) else {}
        if response.ok:
            await channel.send_response(ws, req_id, ok=True, payload=payload)
            return True
        if not is_legacy_shared_directory_client(real_client):
            await channel.send_response(
                ws, req_id, ok=False,
                error=str(payload.get("error") or f"{label} failed"),
                code=payload.get("code") or "BAD_REQUEST",
            )
            return True
        return False

    async def _compact_partial_via_e2a(
        target_sid: str,
        turn_index: int,
        direction: str,
        user_id: str | None = None,
    ) -> tuple[Optional[str], int]:
        """通过 E2A 转发 LLM 摘要请求到 AgentServer。返回 (summary, summarized_count)。"""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        real_client = _resolve_agent_client(agent_client)
        if real_client is None:
            return None, 0

        try:
            env = e2a_from_agent_fields(
                request_id=str(time.time()),
                channel_id="tui",
                session_id=target_sid,
                req_method=ReqMethod.COMMAND_COMPACT_PARTIAL,
                params={
                    "session_id": target_sid,
                    "turn_index": turn_index,
                    "direction": direction,
                },
                is_stream=False,
                timestamp=time.time(),
                user_id=user_id,
            )
            resp = await _send_tui_agent_request(
                real_client, env, label="command.compact_partial",
            )
            if resp.ok:
                pl = resp.payload if isinstance(resp.payload, dict) else {}
                summary = pl.get("summary") if pl.get("status") == "ok" else None
                summarized_count = pl.get("summarized_count", 0)
                return summary, summarized_count
            logger.warning("[compact_partial_via_e2a] E2A failed: %s", resp.payload)
        except Exception as e:
            logger.warning("[compact_partial_via_e2a] E2A call failed: %s", e)

        return None, 0

    async def _session_rewind(ws, req_id, params, session_id, user_id=None):
        """session.rewind: E2A → AgentServer（权威写入者），fallback 本地."""
        from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target_sid = str(params.get("session_id") or session_id or "").strip()
        turn_index = params.get("turn_index")
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        if turn_index is None:
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index is required", code="BAD_REQUEST"
            )
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index must be an integer", code="BAD_REQUEST"
            )
            return

        if await _forward_rewind_e2a(
            ForwardRewindE2AParams(
                ws=ws,
                req_id=req_id,
                target_sid=target_sid,
                turn_index=turn_index,
                req_method=ReqMethod.SESSION_REWIND,
                error_label="session.rewind failed",
                user_id=user_id,
            )
        ):
            return

        try:
            result = rewind_session(session_id=target_sid, turn_index=turn_index)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _history_list_turns(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.agents.harness.common.session_ops_service import list_session_turns
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            params = {}
        target_sid = str(params.get("session_id") or session_id or "").strip()
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        forward_params = dict(params)
        forward_params["session_id"] = target_sid
        if await _forward_tui_unary(
            ws, req_id, forward_params, target_sid, user_id,
            req_method=ReqMethod.HISTORY_LIST_TURNS, label="history.list_turns",
        ):
            return
        try:
            result = list_session_turns(session_id=target_sid)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _session_rewind_and_restore(ws, req_id, params, session_id, user_id=None):
        """session.rewind_and_restore: E2A → AgentServer（权威写入者），fallback 本地."""
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            restore_session_files,
            rewind_session,
        )
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target_sid = str(params.get("session_id") or session_id or "").strip()
        turn_index = params.get("turn_index")
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        if turn_index is None:
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index is required", code="BAD_REQUEST"
            )
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index must be an integer", code="BAD_REQUEST"
            )
            return

        if await _forward_rewind_e2a(
            ForwardRewindE2AParams(
                ws=ws,
                req_id=req_id,
                target_sid=target_sid,
                turn_index=turn_index,
                req_method=ReqMethod.SESSION_REWIND_AND_RESTORE,
                error_label="session.rewind_and_restore failed",
                user_id=user_id,
            )
        ):
            return

        try:
            restore_result = restore_session_files(session_id=target_sid, turn_index=turn_index)
            rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)
            combined = {
                **rewind_result,
                "restored_files": restore_result.get("restored_files", []),
                "deleted_files": restore_result.get("deleted_files", []),
                "restore_errors": restore_result.get("errors", []),
            }
            await channel.send_response(ws, req_id, ok=True, payload=combined)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _session_restore_files(ws, req_id, params, session_id, user_id=None):
        """session.restore_files: 仅恢复文件，不截断对话."""
        from jiuwenswarm.agents.harness.common.session_ops_service import restore_session_files
        from jiuwenswarm.common.schema.message import ReqMethod

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target_sid = str(params.get("session_id") or session_id or "").strip()
        turn_index = params.get("turn_index")
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        if turn_index is None:
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index is required", code="BAD_REQUEST"
            )
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index must be an integer", code="BAD_REQUEST"
            )
            return
        forward_params = dict(params)
        forward_params["session_id"] = target_sid
        forward_params["turn_index"] = turn_index
        if await _forward_tui_unary(
            ws, req_id, forward_params, target_sid, user_id,
            req_method=ReqMethod.SESSION_RESTORE_FILES, label="session.restore_files",
        ):
            return
        try:
            result = restore_session_files(session_id=target_sid, turn_index=turn_index)
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _command_rewind_compact(ws, req_id, params, session_id, user_id=None):
        """command.rewind_compact: LLM 摘要(E2A→AgentServer) + 截断 + 记录写入(AgentServer E2A)。"""
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        target_sid = str(params.get("session_id") or session_id or "").strip()
        turn_index = params.get("turn_index")
        direction = str(params.get("direction") or "from").strip()
        if not target_sid:
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST"
            )
            return
        if turn_index is None:
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index is required", code="BAD_REQUEST"
            )
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            await channel.send_response(
                ws, req_id, ok=False, error="turn_index must be an integer", code="BAD_REQUEST"
            )
            return
        if direction not in ("from", "up_to"):
            await channel.send_response(
                ws, req_id, ok=False, error="direction must be 'from' or 'up_to'", code="BAD_REQUEST"
            )
            return

        from jiuwenswarm.gateway.routing.e2a_proxy import is_agentos_routing_client

        real_client = _resolve_agent_client(agent_client)
        agentos_routing = is_agentos_routing_client(real_client)
        try:
            llm_summary, summarized_count = await _compact_partial_via_e2a(
                target_sid, turn_index, direction, user_id=user_id
            )
        except Exception as e:
            logger.warning("[cli command.rewind_compact] LLM summary failed: %s", e)
            llm_summary = None
            summarized_count = 0

        # Step 2: Send rewind to AgentServer (truncation + agent-internal record writing)
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        if real_client is not None:
            try:
                env = e2a_from_agent_fields(
                    request_id=req_id,
                    channel_id="tui",
                    session_id=target_sid,
                    req_method=ReqMethod.SESSION_REWIND_COMPACT,
                    params={
                        "session_id": target_sid,
                        "turn_index": turn_index,
                        "direction": direction,
                        "compact_summary": llm_summary,
                        "summarized_count": summarized_count,
                    },
                    is_stream=False,
                    timestamp=time.time(),
                    user_id=user_id,
                )
                resp = await _send_tui_agent_request(
                    real_client, env, label="command.rewind_compact",
                )
                if resp.ok:
                    pl = resp.payload if isinstance(resp.payload, dict) else {}
                    pl["summary"] = llm_summary
                    pl["summarized_messages"] = summarized_count
                    await channel.send_response(ws, req_id, ok=True, payload=pl)
                    return
                logger.warning("[cli command.rewind_compact] E2A failed: %s", resp.payload)
            except Exception as e:
                if agentos_routing:
                    await channel.send_response(
                        ws, req_id, ok=False, error=str(e), code="SERVICE_UNAVAILABLE"
                    )
                    return
                logger.warning("[cli command.rewind_compact] E2A failed, fallback local: %s", e)

        if agentos_routing:
            await channel.send_response(
                ws, req_id, ok=False, error="AgentServer is unavailable",
                code="SERVICE_UNAVAILABLE",
            )
            return

        # Fallback: local truncation + record writing
        try:
            from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session
            result = compact_partial_session(
                session_id=target_sid,
                turn_index=turn_index,
                direction=direction,
                llm_summary=llm_summary,
            )
            result["summary"] = llm_summary
            result["summarized_messages"] = summarized_count
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            logger.exception("[cli command.rewind_compact] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _session_rename(ws, req_id, params, session_id, user_id=None):
        """优先经 E2A 转发至 AgentServer；单用户共享目录不可达时由薄代理跑
        SessionAdapter（``apply_session_rename`` 同一中立门面）本地回退。"""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve_agent_client(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_RENAME,
            label="session.rename",
        )

    async def _session_color_set(ws, req_id, params, session_id, user_id=None):
        """设置/查询 session 的 accent_color（统一薄代理：AgentServer 注入目录 metadata）。

        Phase 2 起经 e2a_proxy 转发 SessionAdapter（SESSION_COLOR_SET）；单用户
        WebSocket 客户端在 AgentServer 不可达时由薄代理回落到 Gateway 本地执行
        同一适配器（共享目录），保持迁移前的离线可用性。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve_agent_client(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_COLOR_SET,
            label="session.color_set",
        )

    async def _session_preview(ws, req_id, params, session_id, user_id=None):
        """获取 session 预览信息（统一薄代理：AgentServer 注入目录 history 白名单过滤）。"""
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve_agent_client(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.SESSION_PREVIEW,
            label="session.preview",
        )

    async def _chat_send(ws, req_id, params, session_id):
        await channel.send_response(
            ws, req_id, ok=True, payload={"accepted": True, "session_id": session_id}
        )

    async def _chat_resume(ws, req_id, params, session_id):
        await channel.send_response(
            ws, req_id, ok=True, payload={"accepted": True, "session_id": session_id}
        )

    async def _chat_interrupt(ws, req_id, params, session_id):
        intent = params.get("intent") if isinstance(params, dict) else None
        payload = {"accepted": True, "session_id": session_id}
        if isinstance(intent, str) and intent:
            payload["intent"] = intent
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _tui_disconnect_request(ws, req_id, params, session_id):
        await _cancel_harmonyos_dev_init_tasks(ws)
        payload = {"accepted": True, "session_id": session_id}
        mh = bind.message_handler
        sid = (session_id or "").strip()
        owns_session = True
        is_bound_to_client = getattr(channel, "is_session_bound_to_client", None)
        if callable(is_bound_to_client):
            owns_session = bool(is_bound_to_client("tui", sid, ws))
        cleanup_handed_off = not (mh is not None and sid and owns_session)
        # The disconnect cancel must carry the client's live mode, or the
        # AgentServer routes it through the non-team terminate path and
        # hard-stops running swarmflow runs instead of pausing them. The
        # channel-state table cannot supply it for TUI (TUI is not a control
        # channel), so the tui.disconnect request params are the only source.
        disconnect_mode = ""
        if isinstance(params, dict):
            disconnect_mode = str(params.get("mode") or "").strip()
        if mh is not None and sid and owns_session:
            schedule_cleanup = getattr(
                mh, "schedule_cancel_agent_sessions_on_disconnect", None
            )
            try:
                if callable(schedule_cleanup):
                    await schedule_cleanup(
                        [("tui", sid)],
                        delay_seconds=_TUI_EXPLICIT_EXIT_CANCEL_GRACE_SECONDS,
                        user_id=getattr(ws, "_gateway_user_id", None),
                        mode=disconnect_mode or None,
                    )
                    cleanup_handed_off = True
                else:
                    cleanup_handed_off = bool(
                        await mh.cancel_agent_sessions_on_disconnect(
                            [("tui", sid)],
                            user_id=getattr(ws, "_gateway_user_id", None),
                        )
                    )
            except Exception:
                logger.warning(
                    "[tui.disconnect] cleanup handoff failed; "
                    "transport-close fallback remains enabled: session_id=%s",
                    sid,
                    exc_info=True,
                )

        # The delayed cleanup is registered before acknowledging the exit.
        # A replacement TUI binding cancels it, so cleanup from the old window
        # cannot race with and cancel the newly started session.
        if cleanup_handed_off:
            try:
                setattr(ws, "_jiuwenswarm_tui_user_exit", True)
            except Exception:
                logger.debug("[tui.disconnect] mark user exit failed", exc_info=True)

        try:
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception:
            logger.debug("[tui.disconnect] response skipped on closed ws", exc_info=True)

    async def _chat_user_answer(ws, req_id, params, session_id):
        payload = {"accepted": True, "session_id": session_id}
        request_id = params.get("request_id") if isinstance(params, dict) else None
        if isinstance(request_id, str) and request_id:
            payload["request_id"] = request_id
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _chat_swarmflow_reply(ws, req_id, params, session_id):
        # Empty-ack shell — standard 3-layer routing forwards the reply to the
        # agent adapter, which builds HumanAgentMessage and calls team_manager.
        await channel.send_response(
            ws, req_id, ok=True, payload={"accepted": True, "session_id": session_id}
        )

    async def _history_get(ws, req_id, params, session_id):
        payload = {"accepted": True, "session_id": session_id}
        if isinstance(params, dict):
            if "session_id" in params:
                payload["session_id"] = params.get("session_id")
            if "page_idx" in params:
                payload["page_idx"] = params.get("page_idx")
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _harmonyos_dev_init(ws, req_id, params, session_id, user_id=None):
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        raw_operation_id = params.get("operationId")
        operation_id = str(raw_operation_id or req_id).strip()
        if (
            not operation_id
            or len(operation_id) > 128
            or not all(char.isalnum() or char in "-_." for char in operation_id)
        ):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="invalid HarmonyOS Dev Init operationId",
                code="BAD_REQUEST",
            )
            return

        task_key = (id(ws), operation_id)
        existing = harmonyos_dev_init_tasks.get(task_key)
        if existing is not None and not existing.done():
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=f"HarmonyOS Dev Init operation is already running: {operation_id}",
                code="CONFLICT",
            )
            return

        run_params = dict(params)
        run_params.pop("operationId", None)
        tracked_tasks = _get_harmonyos_dev_init_tasks(ws, create=True)
        if tracked_tasks is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="websocket does not support HarmonyOS Dev Init task tracking",
                code="INTERNAL_ERROR",
            )
            return

        async def _run_harmonyos_dev_init_operation() -> None:
            try:
                from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
                from jiuwenswarm.common.schema.message import ReqMethod
                from jiuwenswarm.gateway.routing.e2a_proxy import (
                    is_legacy_shared_directory_client,
                )

                pl: dict[str, Any] = {}
                real_client = _resolve_agent_client(agent_client)
                try:
                    if real_client is None:
                        await channel.send_response(
                            ws, req_id, ok=False,
                            error="AgentServer is unavailable",
                            code="SERVICE_UNAVAILABLE",
                        )
                        return
                    env = e2a_from_agent_fields(
                        request_id=req_id,
                        channel_id="tui",
                        session_id=session_id,
                        req_method=ReqMethod.HARMONYOS_DEV_INIT,
                        params=run_params,
                        is_stream=False,
                        timestamp=time.time(),
                        user_id=user_id or getattr(ws, "_gateway_user_id", None),
                    )
                    # dev_init 是长时操作（npm install 可达数分钟），豁免 TUI unary
                    # 超时上限，生命周期由本任务的取消机制控制。
                    response = await send_agent_request_with_timeout(
                        real_client,
                        env,
                        label="tui harmonyos.dev_init",
                        timeout_seconds=None,
                    )
                except Exception as exc:
                    # 单用户共享目录回退：默认本地 WebSocket client 与 Gateway
                    # 共用 ~/.jiuwenswarm，传输层不可达时在 Gateway 进程内直接
                    # 执行（与迁移前行为一致）。远程/AgentOS client 不回退，
                    # 向上抛错。业务失败（AgentServer 已正常执行并返回
                    # ok=False）不走此回退，避免本地重复执行长时操作。
                    if not is_legacy_shared_directory_client(real_client):
                        raise
                    logger.warning(
                        "[harmonyos.dev_init] E2A unavailable, fall back to local "
                        "execution: %s",
                        exc,
                    )
                    from jiuwenswarm.server.runtime.harmonyos.harmonyos_dev import (
                        run_harmonyos_dev_init,
                    )

                    pl = await run_harmonyos_dev_init(dict(run_params))
                else:
                    if response.payload is not None and isinstance(response.payload, dict):
                        pl = response.payload
                    if not response.ok:
                        raise RuntimeError(
                            str(pl.get("error") or "harmonyos.dev_init failed")
                        )
                await channel.send_response(ws, req_id, ok=True, payload=pl)
            except asyncio.CancelledError:
                logger.info(
                    "[harmonyos.dev_init] cancelled: operation_id=%s", operation_id
                )
                with contextlib.suppress(Exception):
                    await channel.send_response(
                        ws,
                        req_id,
                        ok=False,
                        error="HarmonyOS Dev Init operation was cancelled",
                        code="CANCELLED",
                    )
                raise
            except Exception as exc:
                logger.warning("[harmonyos.dev_init] failed: %s", exc)
                with contextlib.suppress(Exception):
                    await channel.send_response(
                        ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
                    )
            finally:
                current = asyncio.current_task()
                if harmonyos_dev_init_tasks.get(task_key) is current:
                    harmonyos_dev_init_tasks.pop(task_key, None)

        task = asyncio.create_task(
            _run_harmonyos_dev_init_operation(),
            name=f"harmonyos-dev-init:{operation_id}",
        )
        harmonyos_dev_init_tasks[task_key] = task

        def _forget_task(done_task: asyncio.Task[Any]) -> None:
            if harmonyos_dev_init_tasks.get(task_key) is done_task:
                harmonyos_dev_init_tasks.pop(task_key, None)

        task.add_done_callback(_forget_task)
        tracked_tasks.add(task)
        task.add_done_callback(tracked_tasks.discard)

    async def _harmonyos_dev_init_cancel(ws, req_id, params, session_id):
        del session_id
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        operation_id = str(params.get("operationId") or "").strip()
        if (
            not operation_id
            or len(operation_id) > 128
            or not all(char.isalnum() or char in "-_." for char in operation_id)
        ):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="invalid HarmonyOS Dev Init operationId",
                code="BAD_REQUEST",
            )
            return

        task = harmonyos_dev_init_tasks.get((id(ws), operation_id))
        if task is None or task.done():
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={
                    "operationId": operation_id,
                    "cancelRequested": False,
                    "cancelled": bool(task and task.cancelled()),
                },
            )
            return

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await channel.send_response(
            ws,
            req_id,
            ok=True,
            payload={
                "operationId": operation_id,
                "cancelRequested": True,
                "cancelled": task.cancelled(),
            },
        )

    async def _harmonyos_project_init(ws, req_id, params, session_id, user_id=None):
        """HarmonyOS 项目检查（Phase 3：项目上下文持久化在目标 AgentServer 注入目录）。

        Phase 4 整合：统一薄代理 E2A 转发（HarmonyOSAdapter）；单用户共享目录
        不可达时由薄代理跑同一适配器（保持离线可用）。
        """
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve_agent_client(agent_client),
            ws=ws,
            req_id=req_id,
            params=dict(params),
            session_id=session_id,
            user_id=user_id or getattr(ws, "_gateway_user_id", None),
            req_method=ReqMethod.HARMONYOS_PROJECT_INIT,
            label="harmonyos.project_init",
            default_error_code="INTERNAL_ERROR",
        )

    async def _command_model(ws, req_id, params, session_id, user_id=None):
        # The legacy implementation reads/writes config.yaml directly.
        # Any remote AgentServer has its own user directory.  Only the default
        # local WebSocket client shares Gateway's directory and can use the
        # legacy implementation (sunk to common.config_panel.tui_models_handlers).
        from jiuwenswarm.gateway.routing.e2a_proxy import (
            is_legacy_shared_directory_client,
            proxy_unary_request,
        )

        resolved_client = _resolve_agent_client(agent_client)
        if (
            not force_local_config
            and resolved_client is not None
            and not is_legacy_shared_directory_client(resolved_client)
        ):
            from jiuwenswarm.common.schema.message import ReqMethod

            await proxy_unary_request(
                channel=channel, agent_client=resolved_client, ws=ws,
                req_id=req_id, params=params if isinstance(params, dict) else {},
                session_id=session_id,
                user_id=user_id or getattr(ws, "_gateway_user_id", None),
                req_method=ReqMethod.COMMAND_MODEL, label="command.model",
            )
            return

        await tui_models_handlers.command_model_handler(
            channel, ws, req_id, params, session_id,
            agent_client=agent_client,
            on_config_saved=on_config_saved,
            send_request=_tui_config_send_request,
            user_id=user_id,
        )

    async def _models_list(ws, req_id, params, session_id):
        """models.list 实现已下沉（TUI 契约与 Web 侧分模块保留）。"""
        await tui_models_handlers.models_list_handler(
            channel, ws, req_id, params, session_id
        )

    async def _3rdagent_list(ws, req_id, params, session_id, user_id=None):
        params = params if isinstance(params, dict) else {}
        current = str(
            getattr(ws, "_gateway_agent_type", None)
            or params.get("agent_type")
            or "jiuwenswarm"
        ).strip() or "jiuwenswarm"
        uid = str(user_id or getattr(ws, "_gateway_user_id", None) or "").strip()
        try:
            result = await third_agent.thirdagent_list(
                user_id=uid,
                current_agent_type=current,
                access_mode="tui",
            )
        except Exception as exc:
            logger.warning("[3rdagent.list] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )
            return
        if not result.get("ok"):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(result.get("error") or "3rdagent.list unsupported"),
                code=str(result.get("code") or "UNSUPPORTED"),
            )
            return
        await channel.send_response(
            ws, req_id, ok=True, payload=dict(result.get("payload") or {})
        )

    async def _3rdagent_switch(ws, req_id, params, session_id, user_id=None):
        del session_id  # do not use gateway req_id fallback; require explicit params.session_id
        params = params if isinstance(params, dict) else {}
        uid = str(user_id or getattr(ws, "_gateway_user_id", None) or "").strip()
        explicit_session_id = resolve_3rdagent_switch_session_id(params)
        if not explicit_session_id:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="session_id is required for 3rdagent.switch",
                code="BAD_REQUEST",
            )
            return
        try:
            result = await third_agent.thirdagent_switch(
                user_id=uid,
                agent_type=str(params.get("agent_type") or ""),
                session_id=explicit_session_id,
                params=params,
            )
        except Exception as exc:
            logger.warning("[3rdagent.switch] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )
            return
        if not result.get("ok"):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(result.get("error") or "3rdagent.switch unsupported"),
                code=str(result.get("code") or "UNSUPPORTED"),
            )
            return
        payload = dict(result.get("payload") or {})
        switched = str(payload.get("agent_type") or "").strip()
        if switched:
            setattr(ws, "_gateway_agent_type", switched)
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _proxy_config_request(ws, req_id, params, session_id, user_id=None, *, req_method):
        """Keep CLI configuration on the current AgentServer user directory."""
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve_agent_client(agent_client),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=req_method,
            label="config",
        )

    def _register_config_proxy(method_name, req_method, local_handler):
        async def _handler(ws, req_id, params, session_id, user_id=None):
            # 单用户共享目录：本地执行（保留 on_config_saved 热更新与 TUI 专用键语义）。
            # AgentOS 多用户：经 E2A 在目标 AgentServer 注入目录执行（与 command.model 同模式）。
            from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client

            resolved_client = _resolve_agent_client(agent_client)
            if resolved_client is None or is_legacy_shared_directory_client(resolved_client):
                await local_handler(ws, req_id, params, session_id)
                return
            await _proxy_config_request(
                ws, req_id, params, session_id, user_id, req_method=req_method
            )
        channel.register_local_handler(path, method_name, _handler)

    from jiuwenswarm.common.schema.message import ReqMethod as _ConfigReq
    _register_config_proxy("config.get", _ConfigReq.CONFIG_GET, _config_get)
    _register_config_proxy("config.set", _ConfigReq.CONFIG_SET, _config_set)
    _register_config_proxy("config.validate_model", _ConfigReq.CONFIG_VALIDATE_MODEL, _config_validate_model)
    _register_config_proxy("models.list", _ConfigReq.MODELS_LIST, _models_list)
    channel.register_local_handler(path, "3rdagent.list", _3rdagent_list)
    channel.register_local_handler(path, "3rdagent.switch", _3rdagent_switch)
    channel.register_local_handler(path, "session.list", _session_list)
    channel.register_local_handler(path, "session.create", _session_create)
    channel.register_local_handler(path, "session.rebind_project", _session_rebind_project)
    channel.register_local_handler(path, "session.delete", _session_delete)
    channel.register_local_handler(path, "session.rename", _session_rename)
    channel.register_local_handler(path, "session.color_set", _session_color_set)
    channel.register_local_handler(path, "session.preview", _session_preview)
    channel.register_local_handler(path, "session.rewind", _session_rewind)
    channel.register_local_handler(path, "session.rewind_and_restore", _session_rewind_and_restore)
    channel.register_local_handler(path, "session.restore_files", _session_restore_files)
    channel.register_local_handler(path, "command.rewind_compact", _command_rewind_compact)
    channel.register_local_handler(path, "history.list_turns", _history_list_turns)
    channel.register_local_handler(path, "chat.send", _chat_send)
    channel.register_local_handler(path, "chat.resume", _chat_resume)
    channel.register_local_handler(path, "chat.interrupt", _chat_interrupt)
    channel.register_local_handler(path, "tui.disconnect", _tui_disconnect_request)
    channel.register_local_handler(path, "chat.user_answer", _chat_user_answer)
    channel.register_local_handler(path, "chat.swarmflow_reply", _chat_swarmflow_reply)
    channel.register_local_handler(path, "history.get", _history_get)
    channel.register_local_handler(path, "harmonyos.dev_init", _harmonyos_dev_init)
    channel.register_local_handler(
        path, "harmonyos.dev_init_cancel", _harmonyos_dev_init_cancel
    )
    channel.register_local_handler(path, "harmonyos.project_init", _harmonyos_project_init)
    channel.register_local_handler(path, "command.model", _command_model)

    # ── Hooks RPC handlers ─────────────────────────────────────────────
    async def _hooks_list(ws, req_id, params, session_id, user_id=None):
        # 单用户共享目录：本地读取 config.yaml（hooks 属用户态配置，共享目录等价；
        # AgentServer 未启动/断连时保持可用）。AgentOS：经 E2A 在目标注入目录执行。
        from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client

        resolved_client = _resolve_agent_client(agent_client)
        if resolved_client is None or is_legacy_shared_directory_client(resolved_client):
            from jiuwenswarm.common.hooks_config import load_hooks_config

            try:
                hooks_config = load_hooks_config(get_config())
                summary = hooks_config.get_event_summary()
                await channel.send_response(
                    ws, req_id, ok=True,
                    payload={
                        "events": summary,
                        "disable_all_hooks": hooks_config.disable_all_hooks,
                        "source": "config.yaml",
                    },
                )
            except Exception as exc:
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")
            return

        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

        await proxy_unary_request(
            channel=channel,
            agent_client=_resolve_agent_client(agent_client),
            ws=ws,
            req_id=req_id,
            params={},
            session_id=session_id,
            user_id=user_id or getattr(ws, "_gateway_user_id", None),
            req_method=ReqMethod.HOOKS_LIST,
            label="hooks.list",
        )

    channel.register_local_handler(path, "hooks.list", _hooks_list)

    # ── Memory RPC handlers ────────────────────────────────────────────
    # Phase 3: memory data and its workspace path belong to the target
    # AgentServer.  Gateway/TUI only preserves the RPC protocol and forwards
    # the authenticated routing user_id.  In legacy single-user mode the
    # e2a_proxy transparently falls back to the in-process MemoryAdapter
    # (shared ~/.jiuwenswarm); AgentOS mode returns a retryable error when
    # the target AgentServer is unreachable.
    def _register_memory_proxy(method_name, req_method):
        async def _handler(ws, req_id, params, session_id, user_id=None) -> None:
            """TUI memory 管理转发：与其余用户业务入口统一走 e2a_proxy 薄代理
            （不可达/超时/失败错误映射一致，TUI 通道超时策略经 envelope.channel 生效）。"""
            from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

            await proxy_unary_request(
                channel=channel,
                agent_client=_resolve_agent_client(agent_client),
                ws=ws,
                req_id=req_id,
                params=params if isinstance(params, dict) else {},
                session_id=session_id,
                user_id=user_id,
                req_method=req_method,
                label=req_method.value,
            )
        channel.register_local_handler(path, method_name, _handler)

    from jiuwenswarm.common.schema.message import ReqMethod as _MemoryReq
    _register_memory_proxy("memory.list", _MemoryReq.MEMORY_LIST)
    _register_memory_proxy("memory.edit", _MemoryReq.MEMORY_EDIT)
    _register_memory_proxy("memory.status", _MemoryReq.MEMORY_STATUS)
    _register_memory_proxy("memory.toggle", _MemoryReq.MEMORY_TOGGLE)
    _register_memory_proxy("memory.open", _MemoryReq.MEMORY_OPEN)

    # ── Cron RPC handlers ────────────────────────────────────────────

    def _get_cron():
        """Resolve cron_controller from ref dict or direct instance."""
        if isinstance(cron_controller_ref, dict):
            return cron_controller_ref.get("value")
        return cron_controller_ref

    def _cron_job_field(job, name, default=""):
        """Read a field from ``CronController.get_job`` output (dict or object).

        Real ``get_job`` returns ``CronJob.to_dict()``.  ``getattr`` on a dict
        always yields the default and would reject every authenticated update
        with "job not found".  Keep the object fallback for tests and callers
        that return ``CronJob``-like objects.
        """
        if job is None:
            return default
        if isinstance(job, dict):
            return job.get(name, default)
        return getattr(job, name, default)

    async def _cron_job_list(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        try:
            jobs = await cc.list_jobs()
            await channel.send_response(ws, req_id, ok=True, payload={"jobs": jobs})
        except Exception as exc:
            logger.warning("[cron.job.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _cron_job_meta(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        try:
            await channel.send_response(ws, req_id, ok=True, payload=cc.job_metadata())
        except Exception as exc:
            logger.warning("[cron.job.meta] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _cron_job_get(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            job = await cc.get_job(job_id)
            if job is None:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except Exception as exc:
            logger.warning("[cron.job.get] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _cron_job_create(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        try:
            from jiuwenswarm.gateway.routing.e2a_proxy import (
                is_agentos_routing_client,
                is_legacy_shared_directory_client,
            )

            if session_id:
                params["session_id"] = session_id
            # 与 Web _cron_job_create 对齐：写入创建者，执行时透传 AgentOS X-Session-Context。
            uid = str(user_id or getattr(ws, "_gateway_user_id", None) or "").strip()
            if uid:
                params["user_id"] = uid
            is_agentos = is_agentos_routing_client(_resolve_agent_client(agent_client))
            # 共享目录单用户才可在 Gateway 读取 session metadata 补齐项目目录。
            # AgentOS 用户 session 不在 Gateway 部署目录，不能在这里作本地反查。
            if (
                is_legacy_shared_directory_client(_resolve_agent_client(agent_client))
                and "project_dir" not in params
                and session_id
            ):
                try:
                    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
                    meta = get_session_metadata(session_id, cache_bust=True)
                    if isinstance(meta, dict):
                        pd = meta.get("project_dir")
                        if isinstance(pd, str) and pd.strip():
                            params["project_dir"] = pd.strip()
                except Exception:  # noqa: BLE001
                    pass
            if is_agentos:
                from jiuwenswarm.gateway.routing.e2a_proxy import (
                    resolve_agent_cron_project_binding,
                )

                bound, binding = await resolve_agent_cron_project_binding(
                    agent_client=_resolve_agent_client(agent_client), params=params,
                    user_id=uid or None, channel_id="tui", session_id=session_id,
                )
                if not bound:
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error=str(binding.get("error") or "cron project binding failed"),
                        code=str(binding.get("code") or "SERVICE_UNAVAILABLE"),
                    )
                    return
                resolved_project_id = binding.get("project_id")
                resolved_work_mode = binding.get("work_mode")
                if (
                    not isinstance(resolved_project_id, str)
                    or not isinstance(resolved_work_mode, str)
                    or not resolved_work_mode.strip()
                ):
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error="invalid cron project binding",
                        code="BAD_REQUEST",
                    )
                    return
                params.update({
                    "project_id": resolved_project_id,
                    "work_mode": resolved_work_mode,
                })
                params.pop("project_dir", None)
                params["_agentos_project_binding_verified"] = True
            job = await cc.create_job(params)
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except Exception as exc:
            logger.warning("[cron.job.create] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")

    async def _cron_job_update(ws, req_id, params, session_id, user_id=None):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        patch = params.get("patch") or {}
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        if not isinstance(patch, dict):
            await channel.send_response(ws, req_id, ok=False, error="patch must be object", code="BAD_REQUEST")
            return
        try:
            from jiuwenswarm.gateway.routing.e2a_proxy import is_agentos_routing_client

            patch = dict(patch)
            # 归属校验：与 Web _get_owned_cron_job 语义一致（空 user_id 保持单用户旧行为；
            # 带 user_id 时禁止跨用户读取/更新，包括归属为空的历史 job）。
            uid = str(user_id or getattr(ws, "_gateway_user_id", None) or "").strip()
            existing = None
            if uid:
                existing = await cc.get_job(job_id)
                # 与 Web _get_owned_cron_job 相同的 dict/object 双态读取：
                # 真实 CronController.get_job 返回 to_dict() 的 dict。
                owner_field = _cron_job_field(existing, "user_id", "")
                if existing is None or str(owner_field or "").strip() != uid:
                    await channel.send_response(
                        ws, req_id, ok=False, error="job not found", code="NOT_FOUND"
                    )
                    return
            # 仅当 patch 涉及 project 字段时才解析项目绑定（避免非项目字段的
            # update 因用户侧项目解析失败而被整体拒绝；单用户不 resolve）。
            has_project_fields = "project_id" in patch or "project_dir" in patch
            if (
                is_agentos_routing_client(_resolve_agent_client(agent_client))
                and has_project_fields
            ):
                from jiuwenswarm.gateway.routing.e2a_proxy import (
                    resolve_agent_cron_project_binding,
                )

                if existing is None:
                    existing = await cc.get_job(job_id)
                binding_params = dict(patch)
                binding_params.setdefault(
                    "work_mode", _cron_job_field(existing, "work_mode", "") or "code"
                )
                bound, binding = await resolve_agent_cron_project_binding(
                    agent_client=_resolve_agent_client(agent_client), params=binding_params,
                    user_id=uid or None, channel_id="tui",
                    session_id=_cron_job_field(existing, "session_id", None),
                )
                if not bound:
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error=str(binding.get("error") or "cron project binding failed"),
                        code=str(binding.get("code") or "SERVICE_UNAVAILABLE"),
                    )
                    return
                resolved_project_id = binding.get("project_id")
                resolved_work_mode = binding.get("work_mode")
                if (
                    not isinstance(resolved_project_id, str)
                    or not isinstance(resolved_work_mode, str)
                    or not resolved_work_mode.strip()
                ):
                    await channel.send_response(
                        ws, req_id, ok=False,
                        error="invalid cron project binding",
                        code="BAD_REQUEST",
                    )
                    return
                patch.update({
                    "project_id": resolved_project_id,
                    "work_mode": resolved_work_mode,
                })
                patch.pop("project_dir", None)
                patch["_agentos_project_binding_verified"] = True
            job = await cc.update_job(job_id, patch)
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except KeyError as exc:
            # ZoneInfoNotFoundError is a subclass of KeyError; only treat
            # bare "job not found" KeyError as NOT_FOUND, otherwise surface
            # the real error message.
            if "job not found" in str(exc):
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
            else:
                logger.warning("[cron.job.update] %s", exc)
                await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except Exception as exc:
            logger.warning("[cron.job.update] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")

    async def _cron_job_delete(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            deleted = await cc.delete_job(job_id)
            if not deleted:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            await channel.send_response(ws, req_id, ok=True, payload={"deleted": True})
        except Exception as exc:
            from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError
            code = exc.code if isinstance(exc, LifecycleError) else (getattr(exc, "code", None) or "INTERNAL_ERROR")
            logger.warning("[cron.job.delete] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code=code)

    async def _cron_job_toggle(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        enabled = params.get("enabled", None)
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        if enabled is None:
            await channel.send_response(ws, req_id, ok=False, error="enabled is required", code="BAD_REQUEST")
            return
        try:
            job = await cc.toggle_job(job_id, bool(enabled))
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as exc:
            logger.warning("[cron.job.toggle] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _cron_job_preview(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        count = params.get("count", 5)
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            next_runs = await cc.preview_job(job_id, int(count) if count is not None else 5)
            await channel.send_response(ws, req_id, ok=True, payload={"next": next_runs})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as exc:
            logger.warning("[cron.job.preview] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")

    async def _cron_job_run_now(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            # 先取 job 拿 last_session_id（回退值），再触发 run_now 取 run_id
            # 对齐 chat.send 的 {accepted, session_id} 语义
            job = await cc.get_job(job_id)
            if job is None:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            run_info = await cc.run_now_info(job_id)
            await channel.send_response(
                ws, req_id, ok=True,
                payload={
                    "accepted": True,
                    "run_id": run_info.get("run_id", ""),
                    "session_id": run_info.get("session_id", ""),
                },
            )
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as exc:
            logger.warning("[cron.job.run_now] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    channel.register_local_handler(path, "cron.job.list", _cron_job_list)
    channel.register_local_handler(path, "cron.job.meta", _cron_job_meta)
    channel.register_local_handler(path, "cron.job.get", _cron_job_get)
    channel.register_local_handler(path, "cron.job.create", _cron_job_create)
    channel.register_local_handler(path, "cron.job.update", _cron_job_update)
    channel.register_local_handler(path, "cron.job.delete", _cron_job_delete)
    channel.register_local_handler(path, "cron.job.toggle", _cron_job_toggle)
    channel.register_local_handler(path, "cron.job.preview", _cron_job_preview)
    channel.register_local_handler(path, "cron.job.run_now", _cron_job_run_now)

    # ── Heartbeat RPC handlers(线程续跑,与 cron 独立) ────────────────
    from jiuwenswarm.gateway.heartbeat import HeartbeatServiceUnavailableError

    def _get_heartbeat():
        """Resolve heartbeat_controller from ref dict or direct instance."""
        if isinstance(heartbeat_controller_ref, dict):
            return heartbeat_controller_ref.get("value")
        return heartbeat_controller_ref

    async def _heartbeat_unavailable(ws, req_id, error="heartbeat not available"):
        await channel.send_response(
            ws, req_id, ok=False, error=str(error), code="SERVICE_UNAVAILABLE"
        )

    async def _hb_job_list(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        try:
            result = await hc.list_jobs(
                params if isinstance(params, dict) else {},
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
            await channel.send_response(ws, req_id, ok=True, payload={"jobs": result.get("jobs", [])})
        except ValueError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except PermissionError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="FORBIDDEN")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _hb_job_meta(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        try:
            result = await hc.get_meta(user_id=str(user_id or ""))
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
            return
        await channel.send_response(ws, req_id, ok=True, payload=result)

    async def _hb_job_get(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            job = await hc.get_job(
                job_id,
                access_session_id=session_id,
                user_id=str(user_id or ""),
            )
            if job is None:
                await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
                return
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except PermissionError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="FORBIDDEN")
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.get] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _hb_job_create(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        # TUI 自动继承当前 channel_id=tui + session_id;source=tui_rpc。
        create_params = {
            **params,
            "channel_id": "tui",
            "session_id": session_id,
            "source": "tui_rpc",
        }
        try:
            job = await hc.create_job(
                create_params,
                user_id=str(user_id or getattr(ws, "_gateway_user_id", None) or ""),
            )
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except ValueError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.create] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _hb_job_update(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        patch = params.get("patch")
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        if not isinstance(patch, dict):
            await channel.send_response(ws, req_id, ok=False, error="patch must be object", code="BAD_REQUEST")
            return
        try:
            job = await hc.update_job(
                job_id,
                patch,
                access_session_id=session_id,
                user_id=str(user_id or getattr(ws, "_gateway_user_id", None) or ""),
            )
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except ValueError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except PermissionError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="FORBIDDEN")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.update] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _hb_job_delete(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        try:
            result = await hc.delete_job(
                job_id,
                access_session_id=session_id,
                user_id=str(user_id or getattr(ws, "_gateway_user_id", None) or ""),
            )
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except RuntimeError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="CONFLICT")
        except PermissionError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="FORBIDDEN")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.delete] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _hb_job_toggle(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            await channel.send_response(ws, req_id, ok=False, error="enabled must be boolean", code="BAD_REQUEST")
            return
        try:
            job = await hc.toggle_job(
                job_id,
                enabled,
                access_session_id=session_id,
                user_id=str(user_id or getattr(ws, "_gateway_user_id", None) or ""),
            )
            await channel.send_response(ws, req_id, ok=True, payload={"job": job})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except ValueError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except PermissionError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="FORBIDDEN")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.toggle] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _hb_job_preview(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        count = 5
        raw_count = params.get("count")
        if isinstance(raw_count, int) and raw_count > 0:
            count = raw_count
        try:
            result = await hc.preview_job(
                job_id,
                count=count,
                access_session_id=session_id,
                user_id=str(user_id or getattr(ws, "_gateway_user_id", None) or ""),
            )
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except ValueError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except PermissionError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="FORBIDDEN")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.preview] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _hb_job_run_now(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        reschedule = params.get("reschedule", False)
        if not isinstance(reschedule, bool):
            await channel.send_response(ws, req_id, ok=False, error="reschedule must be boolean", code="BAD_REQUEST")
            return
        try:
            result = await hc.run_now(
                job_id,
                reschedule=reschedule,
                access_session_id=session_id,
                user_id=str(user_id or getattr(ws, "_gateway_user_id", None) or ""),
            )
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except ValueError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")
        except PermissionError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="FORBIDDEN")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.run_now] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _hb_job_cancel(ws, req_id, params, session_id, user_id=None):
        hc = _get_heartbeat()
        if hc is None:
            await _heartbeat_unavailable(ws, req_id)
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        job_id = str(params.get("id") or "").strip()
        if not job_id:
            await channel.send_response(ws, req_id, ok=False, error="id is required", code="BAD_REQUEST")
            return
        pause_schedule = params.get("pause_schedule", False)
        if not isinstance(pause_schedule, bool):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="pause_schedule must be boolean",
                code="BAD_REQUEST",
            )
            return
        try:
            result = await hc.cancel_run(
                job_id,
                pause_schedule=pause_schedule,
                access_session_id=session_id,
                user_id=str(user_id or getattr(ws, "_gateway_user_id", None) or ""),
            )
            await channel.send_response(ws, req_id, ok=True, payload=result)
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except PermissionError as exc:
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="FORBIDDEN")
        except HeartbeatServiceUnavailableError as exc:
            await _heartbeat_unavailable(ws, req_id, exc)
        except Exception as exc:
            logger.warning("[heartbeat.job.cancel] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    channel.register_local_handler(path, "heartbeat.job.list", _hb_job_list)
    channel.register_local_handler(path, "heartbeat.job.meta", _hb_job_meta)
    channel.register_local_handler(path, "heartbeat.job.get", _hb_job_get)
    channel.register_local_handler(path, "heartbeat.job.create", _hb_job_create)
    channel.register_local_handler(path, "heartbeat.job.update", _hb_job_update)
    channel.register_local_handler(path, "heartbeat.job.delete", _hb_job_delete)
    channel.register_local_handler(path, "heartbeat.job.toggle", _hb_job_toggle)
    channel.register_local_handler(path, "heartbeat.job.preview", _hb_job_preview)
    channel.register_local_handler(path, "heartbeat.job.run_now", _hb_job_run_now)
    channel.register_local_handler(path, "heartbeat.job.cancel", _hb_job_cancel)


def build_cli_route_binding(bind: CliRouteBindParams) -> GatewayRouteBinding:
    def _install(channel: Any) -> None:
        # ``GatewayServer`` multiplexes ACP and TUI routes and therefore does
        # not have a class-level channel_id like ``TuiChannel``.  The CLI
        # handlers below use the value when building E2A envelopes; install a
        # route-local value before registering them.
        if not str(getattr(channel, "channel_id", "") or "").strip():
            setattr(channel, "channel_id", bind.channel_id)
        register_cli_handlers(
            CliHandlersBindParams(
                channel=channel,
                agent_client=bind.agent_client,
                message_handler=bind.message_handler,
                third_agent=bind.third_agent,
                on_config_saved=bind.on_config_saved,
                path=bind.path,
                cron_controller=bind.cron_controller,
                heartbeat_controller=bind.heartbeat_controller,
            )
        )

    async def _tui_disconnect(
        _ws: Any,
        stale_session_keys: list[tuple[str, ...]],
        stale_request_keys: list[tuple[str, ...]] | None = None,
    ) -> None:
        await _cancel_harmonyos_dev_init_tasks(_ws)
        mh = bind.message_handler
        cleanup = getattr(mh, "unregister_ws_subscriptions", None)
        ws_id = str(getattr(_ws, "_jiuwen_ws_id", "") or "").strip()
        if callable(cleanup) and ws_id:
            await cleanup(bind.channel_id, ws_id)
        if bool(getattr(_ws, "_jiuwenswarm_tui_user_exit", False)):
            return
        if mh is None:
            return
        # NOTE: do not early-return on empty stale_session_keys; in-flight streams
        # may still be tracked under stale_request_keys even when _session_to_client
        # was overwritten by a later reconnect on the same session_id.
        request_keys = stale_request_keys or []
        if not stale_session_keys and not request_keys:
            return
        _ws_user_id = getattr(_ws, "_gateway_user_id", None)
        if hasattr(mh, "schedule_cancel_agent_sessions_on_disconnect"):
            await mh.schedule_cancel_agent_sessions_on_disconnect(
                stale_session_keys,
                stale_request_keys=request_keys,
                user_id=_ws_user_id,
            )
            return
        await mh.cancel_agent_sessions_on_disconnect(
            stale_session_keys,
            stale_request_keys=request_keys,
            user_id=_ws_user_id,
        )

    def _tui_session_bound(channel_id: str, session_id: str) -> None:
        mh = bind.message_handler
        if mh is None or not hasattr(mh, "cancel_scheduled_disconnect_cancel"):
            return
        mh.cancel_scheduled_disconnect_cancel(channel_id, session_id)

    return GatewayRouteBinding(
        path=bind.path,
        channel_id=bind.channel_id,
        forward_methods=CLI_FORWARD_REQ_METHODS,
        forward_no_local_handler_methods=CLI_FORWARD_NO_LOCAL_HANDLER_METHODS,
        install=_install,
        disconnect_handler=_tui_disconnect,
        session_bind_handler=_tui_session_bound,
        ws_channel=bind.ws_channel,
    )
