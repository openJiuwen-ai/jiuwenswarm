# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentWebSocketServer - Gateway 与 AgentServer 之间的 WebSocket 服务端."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import time
import uuid
import os
import re
import sys
from pathlib import Path
from typing import Any, ClassVar

from jiuwenclaw.local_env_config import (
    apply_env_overrides_to_active,
    effective_tip,
    get_local_config,
    set_os_environ,
)
from jiuwenclaw.agentserver.session_history import (
    enrich_history_messages_session_id,
    read_history_records_for_frontend,
)
from jiuwenclaw.agentserver.gateway_push.wire import build_server_push_wire
from jiuwenclaw.agentserver.tools.acp_output_tools import get_acp_output_manager
from jiuwenclaw.agentserver.tools.client_tool import get_client_tool_manager
from jiuwenclaw.agentserver.agent_manager import AgentManager
from jiuwenclaw.utils import (
    FileTransferStartParams,
    get_agent_sessions_dir,
    get_config_file,
    resolve_tenant_sessions_dir,
)
from jiuwenclaw.e2a.agent_compat import e2a_to_agent_request
from jiuwenclaw.e2a.constants import (
    FILE_TRANSFER_START,
    FILE_TRANSFER_CHUNK,
    FILE_TRANSFER_COMPLETE,
    FILE_TRANSFER_EVENT_TYPES,
)
from jiuwenclaw.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
)
from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
)
from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.schema.hook_event import AgentServerHookEvents
from jiuwenclaw.extensions.types import WsHandlerContext
from jiuwenclaw.agentserver.extensions import get_rail_manager
from jiuwenclaw.agentserver.runtime_scope import RuntimeScopeKey
from jiuwenclaw.agentserver.permissions.patterns import persist_cli_trusted_directory
from jiuwenclaw.schema.hooks_context import (
    AgentServerChatHookContext,
    AgentWsServerStartHookContext,
    AgentReloadConfigHookContext,
)
from jiuwenclaw.agentserver.agent_manager import AgentManager, ACP_DEFAULT_CAPABILITIES
from jiuwenclaw.e2a.acp.protocol import build_acp_session_new_result
from jiuwenclaw.agentserver.permissions.config_rpc import get_permissions_config_req_methods
from jiuwenclaw.agentserver.sandbox_config_rpc import get_sandbox_config_req_methods
from jiuwenclaw.agentserver.tenant_agent_pool import TenantAgentPool
from jiuwenclaw.agentserver.file_transfer_manager import get_file_transfer_manager
from jiuwenclaw.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_allowed_browser_origin,
)
from jiuwenclaw.agentserver.session_id_safe import (
    normalize_safe_session_id,
    resolve_session_dir_under_root,
)
from jiuwenclaw.agentserver.team.exceptions import (
    TeamDissolveConflictError,
    TeamDissolveError,
    TeamDissolveNameMismatchError,
    TeamDissolveUnsupportedError,
)
from jiuwenclaw.agentserver.team.team_manager import get_team_manager


logger = logging.getLogger(__name__)

# 流式处理心跳间隔：当 Agent 处理时间超过此阈值时，发送心跳 chunk 保持 WebSocket 连接活跃
# 避免 ping_timeout 导致连接关闭。默认 10 秒，小于服务端 ping_timeout=20s。
_STREAM_HEARTBEAT_INTERVAL_SECONDS = 10.0

_INTERFACE_DEEP_MODULE = "jiuwenclaw.agentserver.deep_agent.interface_deep"
_interface_deep_warmup_task: asyncio.Task[None] | None = None


def _import_interface_deep_blocking() -> None:
    if _INTERFACE_DEEP_MODULE not in sys.modules:
        importlib.import_module(_INTERFACE_DEEP_MODULE)


async def _warm_interface_deep_import() -> None:
    if _INTERFACE_DEEP_MODULE in sys.modules:
        logger.info("[AgentWebSocketServer] interface_deep_warmup cache=hit elapsed_ms=0.0")
        return
    t0 = time.perf_counter()
    logger.info("[AgentWebSocketServer] interface_deep_warmup cache=miss starting background import")
    try:
        await asyncio.to_thread(_import_interface_deep_blocking)
    except Exception:
        logger.warning(
            "[AgentWebSocketServer] interface_deep_warmup failed elapsed_ms=%.1f",
            (time.perf_counter() - t0) * 1000,
            exc_info=True,
        )
        return
    logger.info(
        "[AgentWebSocketServer] interface_deep_warmup cache=miss ok elapsed_ms=%.1f",
        (time.perf_counter() - t0) * 1000,
    )


def _start_interface_deep_warmup() -> None:
    global _interface_deep_warmup_task
    if _interface_deep_warmup_task is not None and not _interface_deep_warmup_task.done():
        return
    _interface_deep_warmup_task = asyncio.get_running_loop().create_task(_warm_interface_deep_import())

_SYSTEM_PROMPT_USER_HISTORY_PATTERN = re.compile(r"(\[[^\]\n]*用户\]\s*)(.*?)(\s*\[/对话历史\])", re.DOTALL)




def _mask_text_for_log(value: str) -> str:
    return "******" if len(value) <= 20 else f"{value[:5]}******{value[-5:]}"


def _mask_system_prompt_for_log(system_prompt: str) -> str:
    return _SYSTEM_PROMPT_USER_HISTORY_PATTERN.sub(
        lambda match: f"{match.group(1)}{_mask_text_for_log(match.group(2).strip())}{match.group(3)}",
        system_prompt,
    )


def _mask_query_for_log(data: dict[str, Any]) -> dict[str, Any]:
    params = data.get("params")
    if not isinstance(params, dict):
        return data

    masked_params = dict(params)
    query = params.get("query")
    if isinstance(query, str) and query:
        masked_params["query"] = _mask_text_for_log(query)

    system_prompt = params.get("system_prompt")
    if isinstance(system_prompt, str) and system_prompt:
        masked_params["system_prompt"] = _mask_system_prompt_for_log(system_prompt)

    supplementary_info = params.get("supplementary_info")
    if isinstance(supplementary_info, str) and supplementary_info:
        masked_params["supplementary_info"] = _mask_text_for_log(supplementary_info)

    if masked_params == params:
        return data
    return {**data, "params": masked_params}


def _sessions_dir_for_request(request: AgentRequest) -> Path:
    """Resolve tenant sessions root from request agent_id/service_id."""
    agent_id, service_id = TenantAgentPool.extract_ids(request)
    return resolve_tenant_sessions_dir(service_id, agent_id)


def _payload_to_request(data: dict[str, Any]) -> AgentRequest:
    """将 Gateway 发送的 JSON 载荷解析为 AgentRequest."""
    from jiuwenclaw.schema.message import ReqMethod

    req_method = data.get("req_method")
    if req_method is not None and isinstance(req_method, str):
        req_method = ReqMethod(req_method)

    return AgentRequest(
        request_id=data["request_id"],
        channel_id=data.get("channel_id", ""),
        session_id=data.get("session_id"),
        req_method=req_method,
        params=data.get("params", {}),
        is_stream=data.get("is_stream", False),
        timestamp=data.get("timestamp", 0.0),
        metadata=data.get("metadata"),
        agent_id=data.get("agent_id"),
        service_id=data.get("service_id"),
    )



def resolve_agent_request_mode(raw_mode: Any) -> tuple[str, str | None, str]:
    """Resolve request params.mode into manager mode, sub_mode, and canonical value."""
    raw_value = getattr(raw_mode, "value", raw_mode)
    mode_text = raw_value.strip().lower() if isinstance(raw_value, str) else ""
    if not mode_text:
        mode_text = "agent"

    if mode_text in ("plan", "fast"):
        return "agent", None, "agent"

    if mode_text == "agent.team":
        # Canonicalize agent.team to bare team for manager routing.
        return "team", None, "team"

    parts = mode_text.split(".")
    mode = parts[0] or "agent"
    if mode == "agent":
        return "agent", None, "agent"
    if mode == "team":
        sub_mode = parts[1] if len(parts) > 1 and parts[1] else None
        if sub_mode not in {None, "plan"}:
            sub_mode = None
        canonical_mode = f"team.{sub_mode}" if sub_mode else "team"
        if sub_mode == "plan":
            return "code", "team", canonical_mode
        return "team", sub_mode, canonical_mode

    default_sub_modes = {"code": "normal"}
    sub_mode = parts[1] if len(parts) > 1 and parts[1] else default_sub_modes.get(mode)
    if mode == "code" and sub_mode not in {"plan", "normal", "team"}:
        sub_mode = default_sub_modes.get(mode, "normal")
    canonical_mode = f"{mode}.{sub_mode}" if sub_mode else mode
    return mode, sub_mode, canonical_mode


def validate_target_agent_routing(params: dict[str, Any]) -> None:
    """Validate Relay's mutually exclusive single-mention routing fields."""
    target_agent = params.get("target_agent")
    if target_agent is None:
        return
    if not isinstance(target_agent, str) or not target_agent.strip():
        raise ValueError("target_agent must be a non-empty string")

    raw_mode = getattr(params.get("mode"), "value", params.get("mode"))
    normalized_mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
    if normalized_mode != "agent.plan":
        raise ValueError("TARGET_AGENT_MODE_INVALID: target_agent requires agent.plan")

    if str(params.get("team_name") or "").strip():
        raise ValueError(
            "TARGET_AGENT_ROUTE_CONFLICT: target_agent and team_name cannot be used together"
        )

    if params.get("hint_members"):
        raise ValueError(
            "TARGET_AGENT_ROUTE_CONFLICT: target_agent and hint_members cannot be used together"
        )


def validate_team_runtime_identity_params(params: dict[str, Any]) -> None:
    """Reject legacy runtime identity fields on Team requests."""
    mode = str(params.get("mode") or "").strip().lower()
    is_team = bool(params.get("team")) or mode in {
        "team",
        "agent.team",
        "team.plan",
        "code.team",
    }
    if not is_team:
        return
    forbidden = [
        field_name
        for field_name in ("identify", "soul", "system_prompt")
        if params.get(field_name) is not None
    ]
    if forbidden:
        raise ValueError(
            "TEAM_RUNTIME_IDENTITY_NOT_ALLOWED: " + ", ".join(forbidden)
        )


def _read_file_snapshot(path: Path) -> bytes | None:
    """Return a file's bytes, or ``None`` when it does not exist."""
    return path.read_bytes() if path.is_file() else None


def _restore_file_snapshot(path: Path, snapshot: bytes | None) -> None:
    """Restore one file snapshot with an atomic replacement when possible."""
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback.tmp")
    try:
        tmp_path.write_bytes(snapshot)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


_RELAY_TEAM_SCALAR_WHITELIST: tuple[str, ...] = (
    "team_name",
    "lifecycle",
    "team_mode",
    "teammate_mode",
    "dispatch_mode",
    "spawn_mode",
    "enable_swarmflow",
    "max_debate_rounds",
    # Per-team tool/skill deny-lists (generic knobs; what to disable is the
    # caller's policy). Merged over the global react.disabled_tools /
    # disabled_skills at team member assembly.
    "disabled_tools",
    "disabled_skills",
    # enable_permissions is intentionally omitted: team rails follow the
    # global permissions.enabled switch (same as plan), not a team-local flag.
)


def _normalize_relay_team_payload(teams_payload: Any) -> dict[str, Any]:
    """Relay sync payload delimitation: team scalar whitelist + ENT constants.

    - Default ``team_mode`` to ``predefined`` when absent; keep explicit FE values.
    - Always force ``enable_swarmflow=False`` (product policy).
    - Drop team-local ``enable_permissions``; runtime uses global permissions.enabled.
    """
    if not isinstance(teams_payload, dict):
        return teams_payload  # type: ignore[return-value]
    teams_raw = teams_payload.get("team")
    if not isinstance(teams_raw, list):
        return teams_payload
    normalized_teams: list[dict[str, Any]] = []
    for item in teams_raw:
        if not isinstance(item, dict):
            normalized_teams.append(item)
            continue
        filtered: dict[str, Any] = {}
        for key in _RELAY_TEAM_SCALAR_WHITELIST:
            if key in item:
                filtered[key] = item[key]
        filtered.setdefault("team_mode", "predefined")
        filtered["enable_swarmflow"] = False
        for struct_key in ("leader", "teammate", "predefined_members"):
            if struct_key in item:
                filtered[struct_key] = item[struct_key]
        # agents registry is top-level on payload, not per-team; keep any passthrough keys
        # that are not scalars handled above (none expected on team item besides structs).
        normalized_teams.append(filtered)
    result = dict(teams_payload)
    result["team"] = normalized_teams
    return result



class AgentWebSocketServer:
    """Gateway 与 AgentServer 之间的 WebSocket 服务端（多例）.

    监听来自 Gateway (WebSocketAgentServerClient) 的连接，按协议约定处理请求：
    - 收到 JSON：E2AEnvelope（或过渡期 legacy + 兜底信封）
    - is_stream=False：``process_message`` → 一条 **E2AResponse** JSON（``jiuwenclaw.e2a.wire_codec``）
    - is_stream=True：逐条 **E2AResponse** JSON（chunk/complete/error）
    - 例外：首帧 ``connection.ack`` 仍为 ``type/event`` 事件帧

    支持 send_push：推送帧亦为 E2AResponse 线格式（由 chunk 编码）。
    """

    _instance: ClassVar[AgentWebSocketServer | None] = None

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18000,
        *,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 300.0,
    ) -> None:
        self._host = host
        self._port = port
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._server: Any = None
        # 当前 Gateway 连接，用于 send_push 主动推送
        self._current_ws: Any = None
        self._current_send_lock: asyncio.Lock | None = None
        self._acp_client_capabilities_by_ws: dict[int, dict[str, Any]] = {}
        self._agent_manager = None  # TenantAgentPool 实例
        self._agents_sync_config_lock = asyncio.Lock()
        get_acp_output_manager().set_send_push_callback(
            lambda msg: asyncio.create_task(self.send_push(msg))
        )
        get_client_tool_manager().set_send_push_callback(
            lambda msg: asyncio.create_task(self.send_push(msg))
        )

    @staticmethod
    def _ws_capabilities_key(ws: Any) -> int:
        return id(ws)

    def _set_ws_acp_client_capabilities(self, ws: Any, capabilities: dict[str, Any] | None) -> None:
        key = self._ws_capabilities_key(ws)
        if isinstance(capabilities, dict):
            self._acp_client_capabilities_by_ws[key] = dict(capabilities)
        else:
            self._acp_client_capabilities_by_ws.pop(key, None)

    def _get_ws_acp_client_capabilities(self, ws: Any) -> dict[str, Any]:
        key = self._ws_capabilities_key(ws)
        caps = self._acp_client_capabilities_by_ws.get(key)
        return dict(caps) if isinstance(caps, dict) else {}

    def _clear_ws_acp_client_capabilities(self, ws: Any) -> None:
        self._acp_client_capabilities_by_ws.pop(self._ws_capabilities_key(ws), None)

    @classmethod
    def get_instance(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = 18000,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 300.0,
    ) -> "AgentWebSocketServer":
        """返回多例实例。

        首次调用时创建实例，后续调用返回已存在的实例。
        """
        if cls._instance is not None:
            return cls._instance
        cls._instance = cls(
            host=host,
            port=port,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）。"""
        cls._instance = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    # ---------- 生命周期 ----------
    # 端口解析/分配工具已抽到 jiuwenclaw.agentserver.sandbox.port_util (Linux/Windows
    # 共用, 照搬 jiuwenswarm PR #4088), 本类直接调用, 不再内联。

    async def _bootstrap_internal_jiuwenbox(self) -> None:
        """启动时按 ``config.yaml::sandbox`` 自动拉起 jiuwenbox 子进程。"""
        try:
            if sys.platform not in ("linux", "win32"):
                logger.info(
                    "[AgentWebSocketServer] skipping jiuwenbox auto-start: "
                    "sandbox is only supported on Linux/Windows (current: %r)",
                    sys.platform,
                )
                return
            from jiuwenclaw.config import (
                get_sandbox_runtime,
                get_sandbox_startup_mode_explicit,
            )

            # sandbox.enabled 门控: false 时整个跳过 (不拉起 box-server, 不触发 install/
            # 创建 jbx-sandbox 用户). 优先级高于 startup_mode — enabled=false 即使用户配了
            # startup_mode=internal 也不拉起. 默认 false: shipped 模板 enabled=false, 用户
            # 不显式写 sandbox.enabled: true 就不拉起 (opt-in, 避免开箱即 install + 建进程).
            sandbox_runtime = get_sandbox_runtime()
            if not sandbox_runtime.get("enabled"):
                logger.info(
                    "[AgentWebSocketServer] sandbox.enabled=false, "
                    "skipping jiuwenbox auto-start (不拉起 box-server, 不创建沙箱用户). "
                    "如需沙箱, 在 config.yaml 设 sandbox.enabled: true + startup_mode: internal"
                )
                return

            explicit_mode = get_sandbox_startup_mode_explicit()
            if explicit_mode is None:
                logger.info(
                    "[AgentWebSocketServer] sandbox.startup_mode 未在 config.yaml "
                    "中显式配置, skipping jiuwenbox auto-start (如需 agent-server "
                    "自动拉起 jiuwenbox 子进程, 设置 sandbox.startup_mode: internal)"
                )
                return
            if explicit_mode != "internal":
                logger.info(
                    "[AgentWebSocketServer] sandbox.startup_mode=%r, skipping "
                    "jiuwenbox auto-start (external 模式由用户自行拉起 jiuwenbox-server)",
                    explicit_mode,
                )
                return

            from jiuwenclaw.agentserver.sandbox_lifecycle import start_box_server_internal
            await start_box_server_internal()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] jiuwenbox auto-start bootstrap failed: %s", exc,
            )

    async def start(self) -> None:
        """启动 WebSocket 服务端，开始监听连接。优先使用 legacy.server.serve 以与 Gateway 的 legacy client 握手兼容."""
        await self._trigger_before_ws_server_start_hook()
        # 初始化 TenantAgentPool
        if self._agent_manager is None:
            self._agent_manager = TenantAgentPool.get_instance()
            logger.info("[AgentWebSocketServer] 已初始化 TenantAgentPool")

        if self._server is not None:
            logger.warning("[AgentWebSocketServer] 服务端已在运行")
            return

        # 启动文件传输管理器的清理任务
        ft_manager = get_file_transfer_manager()
        if ft_manager.enabled:
            await ft_manager.start_cleanup_task()

        try:
            from websockets.legacy.server import serve as legacy_serve
            self._server = await legacy_serve(
                self._connection_handler,
                self._host,
                self._port,
                process_request=self._process_request,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
            )
        except ImportError:
            import websockets
            self._server = await websockets.serve(
                self._connection_handler,
                self._host,
                self._port,
                process_request=self._process_request,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
            )
        logger.info(
            "[AgentWebSocketServer] 已启动: ws://%s:%s", self._host, self._port,
            extra={'user_visible': 'critical'},
        )
        _start_interface_deep_warmup()
        # 按 config.yaml::sandbox.startup_mode 自动拉起 jiuwenbox 子进程 (internal 模式)。
        await self._bootstrap_internal_jiuwenbox()

    async def _process_request(self, *args: Any) -> Any:
        """在握手阶段执行 Origin 校验，兼容 legacy/new websockets APIs。"""
        path, request_headers = extract_handshake_request(args)
        origin = get_header_value(request_headers, "Origin")

        allowed = is_allowed_browser_origin(origin)
        logger.info(
            "[AgentWebSocketServer] 握手检查 path=%s origin=%s allowed=%s",
            path,
            origin,
            allowed,
        )
        if allowed:
            return None

        logger.warning(
            "[AgentWebSocketServer] 握手拒绝 path=%s origin=%s reason=origin_not_allowed",
            path,
            origin,
        )
        return forbidden_origin_response(args)

    async def stop(self) -> None:
        """停止 WebSocket 服务端."""
        # 停止文件传输管理器的清理任务
        ft_manager = get_file_transfer_manager()
        if ft_manager.enabled:
            await ft_manager.stop_cleanup_task()

        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("[AgentWebSocketServer] 已停止", extra={'user_visible': 'critical'})

    # ---------- 连接处理 ----------

    async def _connection_handler(self, ws: Any) -> None:
        """处理单个 Gateway WebSocket 连接，同一连接可并发处理多个请求."""
        import websockets
        # 和客户端连接成功后, 触发agentserver启动成功的回调事件
        await self._trigger_agent_server_started_hook()

        remote = ws.remote_address
        logger.info("[AgentWebSocketServer] 新连接: %s", remote, extra={'user_visible': 'critical'})

        send_lock = asyncio.Lock()
        self._current_ws = ws
        self._current_send_lock = send_lock

        # 触发身份获取（在发送 connection.ack 之前）
        try:
            from jiuwenclaw.extensions.identity_provider import IdentityStore
            identity = await IdentityStore.get_instance().fetch_and_store()
            if identity is not None:
                logger.info(
                    "[AgentWebSocketServer] 身份信息已获取: user_id=%s domain_id=%s app_id=%s",
                    identity.user_id,
                    identity.domain_id,
                    identity.app_id,
                    extra={'user_visible': 'progress'}
                )
            else:
                logger.debug("[AgentWebSocketServer] 未获取到身份信息（无 provider 或获取失败）",
                             extra={'user_visible': 'progress'})
        except Exception as e:
            logger.warning("[AgentWebSocketServer] 身份获取异常: %s", e, extra={'user_visible': 'progress'})

        # 发送 connection.ack 事件，通知 Gateway 服务端已就绪
        try:
            ack_frame = {
                "type": "event",
                "event": "connection.ack",
                "payload": {"status": "ready"},
            }
            await ws.send(json.dumps(ack_frame, ensure_ascii=False))
            logger.info("[AgentWebSocketServer] 已发送 connection.ack: %s", remote,
                        extra={'user_visible': 'critical'})
        except Exception as e:
            logger.warning("[AgentWebSocketServer] 发送 connection.ack 失败: %s", e,
                           extra={'user_visible': 'critical'})

        tasks: set[asyncio.Task] = set()

        try:
            async for raw in ws:
                task = asyncio.create_task(self._handle_message(ws, raw, send_lock))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except websockets.exceptions.ConnectionClosed as e:
            # websockets>=12: 正式字段为 rcvd/sent；勿用已 deprecate 的 e.code/e.reason
            rcvd = getattr(e, "rcvd", None)
            sent = getattr(e, "sent", None)
            logger.warning(
                "[AgentWebSocketServer] 连接关闭 remote=%s "
                "rcvd_code=%s rcvd_reason=%r sent_code=%s sent_reason=%r "
                "rcvd_then_sent=%s detail=%s",
                remote,
                getattr(rcvd, "code", None),
                getattr(rcvd, "reason", None) or "",
                getattr(sent, "code", None),
                getattr(sent, "reason", None) or "",
                getattr(e, "rcvd_then_sent", None),
                e,
                extra={"user_visible": "critical"},
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] 连接处理异常 (%s): %s", remote, e,
                             extra={'user_visible': 'critical'})
        finally:
            self._current_ws = None
            self._current_send_lock = None
            self._clear_ws_acp_client_capabilities(ws)
            # Gateway 进程退出/端口关闭时，必须先取消各 session 内流式生产者（SessionManager）
            # 并中止 DeepAgent 内层循环；否则仅等待 _handle_message 任务结束会一直阻塞到任务自然完成。
            try:
                await self._agent_manager.cancel_all_inflight_work(
                    reason=f"[gateway ws closed {remote}] ",
                )
            except Exception:
                logger.exception("[AgentWebSocketServer] cancel_all_inflight_work failed",
                                 extra={'user_visible': 'progress'})
            try:
                # Align with plan disconnect: stop in-flight team work but park
                # runtime so the same session can continue after reconnect.
                from jiuwenclaw.agentserver.team import (
                    pause_all_team_session_runtimes_across_managers,
                )

                await pause_all_team_session_runtimes_across_managers(
                    reason=f"[gateway ws closed {remote}] ",
                )
            except Exception:
                logger.exception(
                    "[AgentWebSocketServer] team session pause on disconnect failed",
                    extra={'user_visible': 'progress'},
                )
            if tasks:
                for t in list(tasks):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _get_custom_ws_handler(method: str):
        """检查 method 是否为已注册的自定义 WebSocket 处理器。"""
        if not method:
            return None
        try:
            from jiuwenclaw.extensions.registry import ExtensionRegistry
            return ExtensionRegistry.get_instance().get_ws_handler(method)
        except RuntimeError:
            # ExtensionRegistry 未初始化
            return None

    async def _handle_message(self, ws: Any, raw: str | bytes, send_lock: asyncio.Lock) -> None:
        """解析一条 JSON 请求并分发到 IAgentServer 处理."""
        try:
            data = json.loads(raw)
            logger.info(
                "[AgentWebSocketServer] Inbound raw payload: %s",
                _mask_query_for_log(data),
                extra={'user_visible': 'critical'}
            )
            logger.info(
                f"[AgentWebSocketServer] Inbound raw payload json 解析成功: request_id={data.get('request_id', '')}"
            )
        except json.JSONDecodeError as e:
            logger.error(
                f"[AgentWebSocketServer] Inbound raw payload json 解析失败: error={str(e)}"
            )
            wire = encode_json_parse_error_wire(
                request_id="",
                channel_id="",
                message=f"Inbound raw payload json 解析失败: {e}",
            )
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))
            return

        try:
            env = E2AEnvelope.from_dict(data)
            logger.info(
                f"[AgentWebSocketServer] Inbound raw payload E2A协议解析成功: request_id={env.request_id}"
            )
        except Exception as parse_err:
            logger.warning(
                "[AgentWebSocketServer] Inbound raw payload E2A协议解析失败，按旧 payload 解析: %s",
                parse_err,
            )
            request = _payload_to_request(data)
        else:
            jw = (env.channel_context or {}).get(E2A_INTERNAL_CONTEXT_KEY)
            if isinstance(jw, dict) and jw.get(E2A_FALLBACK_FAILED_KEY):
                legacy = jw.get(E2A_LEGACY_AGENT_REQUEST_KEY)
                logger.warning(
                    "[E2A][fallback] using legacy_agent_request request_id=%s",
                    env.request_id,
                )
                if not isinstance(legacy, dict):
                    raise ValueError("legacy_agent_request missing or not a dict")
                request = _payload_to_request(legacy)
            # 文件传输请求：method 不在 ReqMethod 枚举中，需要特殊处理
            elif env.method in (FILE_TRANSFER_START, FILE_TRANSFER_CHUNK, FILE_TRANSFER_COMPLETE):
                logger.info(
                    "[E2A][in] request_id=%s channel=%s method=%s is_stream=%s",
                    env.request_id,
                    env.channel,
                    env.method,
                    env.is_stream,
                )
                # 直接构造 AgentRequest，不走 e2a_to_agent_request
                request = AgentRequest(
                    request_id=env.request_id or "",
                    channel_id=env.channel or "",
                    session_id=env.session_id,
                    req_method=None,  # 文件传输没有对应的 ReqMethod
                    params=dict(env.params or {}),
                    is_stream=False,
                    timestamp=0.0,
                    metadata=None,
                )
            else:
                logger.info(
                    "[E2A][in] request_id=%s channel=%s method=%s is_stream=%s",
                    env.request_id,
                    env.channel,
                    env.method,
                    env.is_stream,
                )
                # 检查是否为自定义 WebSocket 处理器
                raw_method = env.method or ""
                custom_entry = self._get_custom_ws_handler(raw_method)
                if custom_entry:
                    # 自定义处理器：直接构造 AgentRequest，不需要 ReqMethod
                    request = AgentRequest(
                        request_id=env.request_id or "",
                        channel_id=env.channel or "",
                        session_id=env.session_id,
                        req_method=None,
                        params=dict(env.params or {}),
                        is_stream=False,
                        timestamp=0.0,
                        metadata={"_custom_ws_handler_entry": custom_entry},
                    )
                else:
                    request = e2a_to_agent_request(env)

        logger.info(
            "[AgentWebSocketServer] 收到请求: request_id=%s channel_id=%s is_stream=%s",
            request.request_id,
            request.channel_id,
            request.is_stream,
            extra={'user_visible': 'critical'}
        )

        from jiuwenclaw.interface_resp import maybe_track_e2a_resp

        async with maybe_track_e2a_resp(
            ws,
            req_method=request.req_method,
            params=request.params if isinstance(request.params, dict) else None,
            request_id=request.request_id,
            channel_id=request.channel_id or "",
            session_id=request.session_id,
        ):
            await self._handle_agent_request_body(ws, request, send_lock)

    async def _handle_agent_request_body(self, ws: Any, request: Any, send_lock: asyncio.Lock) -> None:
        from jiuwenclaw.schema.message import ReqMethod

        try:
            if request.channel_id == "acp" and request.req_method != ReqMethod.INITIALIZE:
                metadata = dict(request.metadata or {})
                ws_caps = self._get_ws_acp_client_capabilities(ws)
                metadata.setdefault(
                    "acp_client_capabilities",
                    ws_caps or self._agent_manager.get_client_capabilities("acp"),
                )
                request.metadata = metadata

            # 检查是否为自定义 WebSocket 处理器（在 ReqMethod 路由之前）
            from jiuwenclaw.extensions.types import WsHandlerEntry

            custom_entry_from_meta = request.metadata.get("_custom_ws_handler_entry") if request.metadata else None
            if isinstance(custom_entry_from_meta, WsHandlerEntry):
                ctx = WsHandlerContext(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    params=request.params,
                    metadata=request.metadata,
                )
                # 清除内部标记
                if request.metadata:
                    request.metadata.pop("_custom_ws_handler_entry", None)
                await self._handle_custom_ws_handler(ws, request, custom_entry_from_meta, ctx, send_lock)
                return

            await self._trigger_before_chat_request_hook(request)

            if request.req_method == ReqMethod.SESSION_LIST:
                logger.info(f"[AgentWebSocketServer] 处理 session.list: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_session_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_RENAME:
                logger.info(f"[AgentWebSocketServer] 处理 session.rename: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_session_rename(ws, request, send_lock)
                return
            if request.req_method in get_permissions_config_req_methods():
                logger.info(f"[AgentWebSocketServer] 处理 permissions.config: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_permissions_config(ws, request, send_lock)
                return
            if request.req_method in get_sandbox_config_req_methods():
                logger.info(f"[AgentWebSocketServer] 处理 sandbox.config: request_id={request.request_id}",
                           extra={'user_visible': 'progress'})
                await self._handle_sandbox_config(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HISTORY_GET:
                logger.info(f"[AgentWebSocketServer] 处理 history.get: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                if request.is_stream:
                    await self._handle_history_get_stream(ws, request, send_lock)
                else:
                    await self._handle_history_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_ADD_DIR:
                logger.info(f"[AgentWebSocketServer] 处理 command.add_dir: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_add_dir(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_CHROME:
                logger.info(f"[AgentWebSocketServer] 处理 command.chrome: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_chrome(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_COMPACT:
                logger.info(f"[AgentWebSocketServer] 处理 command.compact: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_compact(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_DIFF:
                logger.info(f"[AgentWebSocketServer] 处理 command.diff: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_diff(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_LS:
                logger.info(f"[AgentWebSocketServer] 处理 command.ls: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_ls(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_VIEW:
                logger.info(f"[AgentWebSocketServer] 处理 command.view: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_view(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_MODEL:
                logger.info(f"[AgentWebSocketServer] 处理 command.model: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_model(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_RESUME:
                logger.info(f"[AgentWebSocketServer] 处理 command.resume: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_resume(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_SESSION:
                logger.info(f"[AgentWebSocketServer] 处理 command.session: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_command_session(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.BROWSER_START:
                logger.info(f"[AgentWebSocketServer] 处理 browser.start: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_browser_start(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.BROWSER_RUNTIME_RESTART:
                logger.info(f"[AgentWebSocketServer] 处理 browser.runtime_restart: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_browser_runtime_restart(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.CONFIG_CACHE_CLEAR:
                logger.info(f"[AgentWebSocketServer] 处理 config.cache_clear: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_config_cache_clear(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENT_RELOAD_CONFIG:
                logger.info(f"[AgentWebSocketServer] 处理 agent.reload_config: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_agent_reload_config(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_CATALOG_LIST:
                logger.info(
                    "[AgentWebSocketServer] 处理 team.catalog.list: request_id=%s",
                    request.request_id,
                    extra={'user_visible': 'progress'},
                )
                await self._handle_team_catalog_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SYNC_AGENTS_CONFIGS:
                logger.info(
                    "[AgentWebSocketServer] 处理 sync_agents_configs: request_id=%s",
                    request.request_id,
                    extra={'user_visible': 'progress'},
                )
                await self._handle_sync_agents_configs(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_LIST:
                logger.info(f"[AgentWebSocketServer] 处理 extensions.list: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_extensions_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_IMPORT:
                logger.info(f"[AgentWebSocketServer] 处理 extensions.import: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_extensions_import(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_DELETE:
                logger.info(f"[AgentWebSocketServer] 处理 extensions.delete: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_extensions_delete(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_TOGGLE:
                logger.info(f"[AgentWebSocketServer] 处理 extensions.toggle: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_extensions_toggle(ws, request, send_lock)
                return
            # 文件传输处理
            event_type = request.params.get("event_type") if isinstance(request.params, dict) else None
            if event_type in FILE_TRANSFER_EVENT_TYPES:
                logger.info(f"[AgentWebSocketServer] 处理 file_transfer: event_type={event_type}, \
                            request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_file_transfer(ws, request, send_lock)
                return
            if request.is_stream:
                logger.info(f"[AgentWebSocketServer] 处理 chat 流式请求: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_stream(ws, request, send_lock)
            else:
                logger.info(f"[AgentWebSocketServer] 处理 chat 非流式请求: request_id={request.request_id}",
                            extra={'user_visible': 'progress'})
                await self._handle_unary(ws, request, send_lock)
        except Exception as e:
            logger.exception(
                "[AgentWebSocketServer] 处理请求失败: request_id=%s: %s",
                request.request_id,
                e,
                extra={'user_visible': 'critical'}
            )
            error_resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(
                error_resp, response_id=request.request_id
            )
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

    @staticmethod
    async def _trigger_before_ws_server_start_hook() -> None:
        """在首次启动之前触发扩展；未初始化 ExtensionRegistry 时跳过。"""
        from jiuwenclaw.extensions.registry import ExtensionRegistry
        from jiuwenclaw.utils import get_agent_skills_dir

        ctx = AgentWsServerStartHookContext(skills_dir=str(get_agent_skills_dir()))
        await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.BEFORE_WS_SERVER_START, ctx)

    @staticmethod
    async def _trigger_agent_server_started_hook() -> None:
        """在agentserver启动成功触发扩展；未初始化 ExtensionRegistry 时跳过。"""
        from jiuwenclaw.extensions.registry import ExtensionRegistry
        from jiuwenclaw.utils import get_agent_skills_dir

        ctx = AgentWsServerStartHookContext(skills_dir=str(get_agent_skills_dir()))
        await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.AGENT_SERVER_STARTED, ctx)

    @staticmethod
    def _should_trigger_before_chat_request_hook(request: AgentRequest) -> bool:
        from jiuwenclaw.schema.message import ReqMethod

        return request.req_method in (
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_RESUME,
            ReqMethod.CHAT_ANSWER,
        )

    async def _trigger_before_chat_request_hook(self, request: AgentRequest) -> None:
        if not self._should_trigger_before_chat_request_hook(request):
            return
        from jiuwenclaw.extensions.registry import ExtensionRegistry

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params

        ctx = AgentServerChatHookContext(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            req_method=request.req_method.value if request.req_method is not None else None,
            params=params,
        )

        await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.BEFORE_CHAT_REQUEST, ctx)

    async def _handle_custom_ws_handler(
        self,
        ws: Any,
        request: AgentRequest,
        entry,  # WsHandlerEntry
        ctx: WsHandlerContext,
        send_lock: asyncio.Lock,
    ) -> None:
        """调用自定义 WebSocket 处理器并返回响应。"""
        logger.info(
            "[AgentWebSocketServer] 调用自定义处理器: method=%s request_id=%s",
            entry.method,
            request.request_id,
        )

        try:
            payload = await entry.handler(ctx)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
                metadata=ctx.response_metadata,
            )
        except Exception as e:
            logger.exception(
                "[AgentWebSocketServer] 自定义处理器异常: method=%s request_id=%s: %s",
                entry.method,
                request.request_id,
                e,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e), "error_type": type(e).__name__},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))
        logger.info(
            "[AgentWebSocketServer] 自定义处理器响应已发送: request_id=%s ok=%s",
            request.request_id,
            resp.ok,
            extra={'user_visible': 'progress'}
        )

    async def _handle_unary(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """非流式处理：调用 process_message，返回一条 E2AResponse 线 JSON。"""
        from jiuwenclaw.schema.message import ReqMethod

        channel_id = request.channel_id or "default"

        if request.req_method == ReqMethod.INITIALIZE:
            await self._handle_initialize(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_CREATE:
            await self._handle_session_create(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_DELETE:
            await self._handle_session_delete(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.TEAM_RUNTIME_DISSOLVE:
            await self._handle_team_runtime_dissolve(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.ACP_TOOL_RESPONSE:
            await self._handle_acp_tool_response(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.CLIENT_TOOL_RESPONSE:
            await self._handle_client_tool_response(ws, request, send_lock)
            return

        resp = await self._agent_manager.process_message(request)

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))
        logger.info(
            "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
            request.request_id,
            extra={'user_visible': 'progress'}
        )

    async def _handle_stream(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """流式处理：调用 process_message_stream，逐条发送 E2AResponse 线 JSON。"""
        # 诊断日志：确认 _handle_stream 是否被调用
        logger.info(
            "[AgentWebSocketServer] DIAGNOSTIC: _handle_stream 开始执行 "
            "| request_id=%s | is_stream=%s | channel=%s",
            request.request_id,
            request.is_stream,
            request.channel_id,
        )
        channel_id = request.channel_id or "default"

        chunk_count = 0
        # 心跳控制：当有真实 chunk 发送时重置，空闲时发送心跳
        heartbeat_event = asyncio.Event()
        heartbeat_task: asyncio.Task | None = None

        async def _heartbeat_loop() -> None:
            """后台心跳任务：在空闲期间定期发送 keepalive chunk."""
            try:
                while True:
                    # 等待心跳间隔，如果期间有真实 chunk 发送则 heartbeat_event 被设置，重置等待
                    try:
                        await asyncio.wait_for(
                            heartbeat_event.wait(),
                            timeout=_STREAM_HEARTBEAT_INTERVAL_SECONDS,
                        )
                        # 有真实 chunk 发送，重置 event 继续等待
                        heartbeat_event.clear()
                    except asyncio.TimeoutError:
                        # 超时：空闲超过心跳间隔，发送 keepalive chunk
                        heartbeat_chunk = AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=channel_id,
                            payload={"event_type": "keepalive"},
                            is_complete=False,
                        )
                        wire = encode_agent_chunk_for_wire(
                            heartbeat_chunk,
                            response_id=request.request_id,
                            sequence=-1,  # 心跳使用特殊序列号 -1
                        )
                        async with send_lock:
                            await ws.send(json.dumps(wire, ensure_ascii=False))
                        logger.debug(
                            "[AgentWebSocketServer] keepalive chunk 发送: request_id=%s",
                            request.request_id,
                        )
            except asyncio.CancelledError:
                pass

        # 启动心跳任务
        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        try:
            async for chunk in self._agent_manager.process_message_stream(request):
                chunk_count += 1

                # 流式响应开始标记（第一个chunk）
                if chunk_count == 1:
                    logger.info(
                        f"[AgentWebSocketServer] 流式响应开始: request_id={request.request_id}",
                        extra={'user_visible': 'critical'}
                    )
                # 流式响应进度标记（每10个chunk）
                elif chunk_count % 10 == 0:
                    logger.info(
                        f"[AgentWebSocketServer] 流式响应进度: request_id={request.request_id} chunk_count={chunk_count}"
                    )

                # 通知心跳任务有真实 chunk 发送，重置心跳计时
                heartbeat_event.set()
                wire = encode_agent_chunk_for_wire(
                    chunk,
                    response_id=request.request_id,
                    sequence=chunk_count - 1,
                )
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                # 清除 event，让心跳任务重新开始计时
                heartbeat_event.clear()
        finally:
            # 停止心跳任务
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        logger.info(
            "[AgentWebSocketServer] 流式响应已发送: request_id=%s 共 %s 个 chunk",
            request.request_id,
            chunk_count,
        )

    async def _handle_session_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 session.list 请求：扫描 sessions 目录，返回历史会话基础信息列表.

        使用 TenantAgentPool.extract_ids 获取租户 ID，默认为 ('default', 'default')。
        """
        from jiuwenclaw.agentserver.session_metadata import get_session_metadata

        # extract_ids 现在总是返回有效值（默认或指定的 tenant ID）
        sessions_dir = _sessions_dir_for_request(request)
        sessions = []

        try:
            if sessions_dir.exists():
                for entry in sorted(sessions_dir.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True):
                    if not entry.is_dir():
                        continue
                    meta = get_session_metadata(entry.name, sessions_root=sessions_dir)
                    if not meta:
                        meta = {
                            "session_id": entry.name,
                            "channel_id": "",
                            "title": "",
                            "message_count": 0,
                            "last_message_at": entry.stat().st_mtime,
                        }
                    sessions.append(meta)
        except Exception as exc:
            logger.warning("[AgentWebSocketServer] 扫描 sessions 目录失败: %s", exc)

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"sessions": sessions},
            metadata=request.metadata,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_session_rename(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 session.rename：与 CLI Gateway 本地回退共用 apply_session_rename。"""
        from jiuwenclaw.agentserver.session_rename import apply_session_rename

        sid = request.session_id or ""
        ch = (request.channel_id or "").strip() or "tui"
        ok, payload, err, code = apply_session_rename(
            request.params,
            sid,
            init_channel_id=ch,
            sessions_root=_sessions_dir_for_request(request),
        )
        if ok:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload or {},
                metadata=request.metadata,
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": err or "session.rename failed", "code": code or ""},
                metadata=request.metadata,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_permissions_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 permissions.* E2A 请求（与 Web ``register_method`` 同名 method）。"""
        from jiuwenclaw.agentserver.permissions.config_rpc import dispatch_permissions_config_request

        pool = self._agent_manager
        catalog_fn = pool.collect_runtime_tools_catalog_nowait if pool is not None else None
        resp = dispatch_permissions_config_request(
            request,
            get_runtime_tools_catalog=catalog_fn,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_sandbox_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 sandbox.* 配置 E2A 请求 (officeAce 经 WS 控制沙箱开关/启动方式/文件/网络)."""
        from jiuwenclaw.agentserver.sandbox_config_rpc import dispatch_sandbox_config_request
        from jiuwenclaw.schema.message import ReqMethod

        resp = dispatch_sandbox_config_request(request)
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

        # 沙箱开关变更后必须 reload_agent_config, 否则同 session 已活着的
        # ReActAgent 仍持有旧 SANDBOX sysop id (已被 teardown 从 resource_mgr
        # 移除), 后续工具调用拿到 None sysop 表现为无权限.
        # 新 session 不受影响: 它懒创建时直接读新 enabled 值造 LOCAL sysop.
        if resp.ok and request.req_method == ReqMethod.SANDBOX_ENABLED_SET:
            try:
                from jiuwenclaw.config import get_config
                config_base = get_config()
                await self._agent_manager.reload_agents_config(config_base, None)
                logger.info(
                    "[AgentWebSocketServer] sandbox.enabled.set 后触发 reload_agents_config, "
                    "同 session ReActAgent 将切到新 sysop",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] sandbox.enabled.set 后 reload_agents_config 失败: %s",
                    exc,
                )

    async def _handle_history_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        data = self.get_conversation_history(
            session_id=session_id,
            page_idx=page_idx,
            sessions_root=_sessions_dir_for_request(request),
        )
        if data is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "invalid page_idx or session history not found"},
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=data,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_history_get_stream(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        data = self.get_conversation_history(
            session_id=session_id,
            page_idx=page_idx,
            sessions_root=_sessions_dir_for_request(request),
        )
        if data is None:
            err_chunk = AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.error",
                    "error": "invalid page_idx or session history not found",
                },
                is_complete=True,
            )
            wire = encode_agent_chunk_for_wire(
                err_chunk,
                response_id=request.request_id,
                sequence=0,
            )
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))
            return

        messages = data.get("messages", [])
        total_pages = data.get("total_pages")
        page = data.get("page_idx")
        if isinstance(messages, list):
            for seq, item in enumerate(messages):
                chunk = AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={
                        "event_type": "history.message",
                        "message": item,
                        "total_pages": total_pages,
                        "page_idx": page,
                    },
                    is_complete=False,
                )
                wire = encode_agent_chunk_for_wire(
                    chunk,
                    response_id=request.request_id,
                    sequence=seq,
                )
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))

        done_chunk = AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "history.message",
                "status": "done",
                "total_pages": total_pages,
                "page_idx": page,
            },
            is_complete=True,
        )
        done_seq = len(messages) if isinstance(messages, list) else 0
        wire_done = encode_agent_chunk_for_wire(
            done_chunk,
            response_id=request.request_id,
            sequence=done_seq,
        )
        async with send_lock:
            await ws.send(json.dumps(wire_done, ensure_ascii=False))

    async def _handle_command_add_dir(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            directory_path = params.get("path")
            remember = params.get("remember", False)
            persist: dict[str, Any]
            if directory_path is None or (
                isinstance(directory_path, str) and not directory_path.strip()
            ):
                persist = {"ok": False, "error": "path is required"}
            else:
                persist = persist_cli_trusted_directory(str(directory_path))
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=bool(persist.get("ok", False)),
                payload={
                    "path": directory_path,
                    "remember": remember,
                    "persist": persist,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.add_dir failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_chrome(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.chrome failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_ls(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from datetime import datetime
        from jiuwenclaw.agentserver.session_metadata import get_resolved_project_dir
        logger.info("[AgentWebSocketServer] command.ls %s", request.params)
        try:
            params = request.params or {}
            relative_path = str(params.get("path", ".")).strip()

            session_id = request.session_id or "default"
            workspace_dir = Path(
                get_resolved_project_dir(session_id, _sessions_dir_for_request(request))
            )
            logger.info("[AgentWebSocketServer] command.ls workspace_dir: %s", workspace_dir)
            target_path = (workspace_dir / relative_path).resolve()
            logger.info("[AgentWebSocketServer] command.ls target_path: %s", target_path)

            try:
                target_path.relative_to(workspace_dir.resolve())
            except ValueError:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Path outside workspace"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            entries = []
            if target_path.is_dir():
                for item in sorted(target_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    try:
                        stat = item.stat()
                        entries.append({
                            "name": item.name,
                            "is_dir": item.is_dir(),
                            "size": stat.st_size if item.is_file() else None,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        })
                    except OSError as e:
                        entries.append({
                            "name": item.name,
                            "is_dir": item.is_dir(),
                            "error": str(e),
                        })
            logger.info("[AgentWebSocketServer] command.ls entries: %s", entries)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "path": str(target_path),
                    "relative_path": relative_path,
                    "entries": entries,
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.ls failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_view(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenclaw.agentserver.session_metadata import get_resolved_project_dir
        logger.info("[AgentWebSocketServer] command.view %s", request.params)
        try:
            params = request.params or {}
            relative_path = str(params.get("path", "")).strip()
            from_line = int(params.get("from_line", 1))
            lines = params.get("lines")

            if not relative_path:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Missing path parameter"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            session_id = request.session_id or "default"
            workspace_dir = Path(
                get_resolved_project_dir(session_id, _sessions_dir_for_request(request))
            )
            target_path = (workspace_dir / relative_path).resolve()

            try:
                target_path.relative_to(workspace_dir.resolve())
            except ValueError:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Path outside workspace"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            if not target_path.is_file():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": f"Not a file: {relative_path}"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
            except UnicodeDecodeError:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Binary file, cannot display"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            start_idx = max(0, from_line - 1)
            if lines is not None and lines > 0:
                end_idx = min(len(all_lines), start_idx + lines)
            else:
                end_idx = len(all_lines)

            selected_lines = all_lines[start_idx:end_idx]

            numbered = []
            for i, line in enumerate(selected_lines, start=start_idx + 1):
                numbered.append(f"{i:4d} | {line.rstrip()}")
            numbered_content = '\n'.join(numbered)
            summary = (
                f"\n\n---\n"
                f"📄 文件: `{relative_path}`\n"
                f"📊 总行数: {len(all_lines)}, 显示: {len(selected_lines)} 行 "
                f"(第 {start_idx + 1}-{end_idx} 行)"
            )

            content = f"```\n{numbered_content}\n```{summary}"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "path": str(target_path),
                    "relative_path": relative_path,
                    "content": content,
                    "from_line": from_line,
                    "total_lines": len(all_lines),
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.view failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_compact(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            custom_instructions = params.get("instructions")
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"instructions": custom_instructions},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.compact failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_diff(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenclaw.agentserver.diff_service import DiffService, get_diff_service

        try:
            session_id = request.session_id or "default"
            agent_id, service_id = TenantAgentPool.extract_ids(request)
            params = request.params if isinstance(request.params, dict) else {}
            # params.project_dir 不可信：仅当与 session metadata 绑定路径一致时才采用
            requested_project_dir = params.get("project_dir")
            if not isinstance(requested_project_dir, str) or not requested_project_dir.strip():
                requested_project_dir = None
            else:
                requested_project_dir = requested_project_dir.strip()
            project_dir = DiffService.resolve_trusted_project_dir(
                session_id,
                requested_project_dir,
                sessions_root=_sessions_dir_for_request(request),
            )
            diff_service = get_diff_service()
            turns = diff_service.get_turn_diffs(
                session_id,
                service_id=service_id,
                agent_id=agent_id,
                project_dir=project_dir,
            )

            logger.info(
                "[AgentWebSocketServer] command.diff response: session_id=%s turns=%d files=%s",
                session_id,
                len(turns),
                {t["turnIndex"]: list(t["files"].keys()) for t in turns},
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "type": "list",
                    "turns": turns,
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.diff failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_model(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            if request.channel_id == "officeclaw":
                guard = TenantAgentPool.require_officeclaw_agent(request)
                if guard is not None:
                    resp = guard
                    wire = encode_agent_response_for_wire(
                        resp, response_id=request.request_id
                    )
                    async with send_lock:
                        await ws.send(json.dumps(wire, ensure_ascii=False))
                    return

            params = request.params or {}
            action = params.get("action")

            if action == "add_model":
                target = str(params.get("target", "")).strip()
                logger.info("[command.model] add_model: target=%s", target)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "model_added", "name": target},
                )

            elif action == "switch_model":
                target = str(params.get("model", "")).strip()
                env_updates = params.get("env_updates", {})
                logger.info(
                    "[command.model] switch_model: target=%s, env_updates=%s",
                    target,
                    {k: (v if k != "API_KEY" else "***") for k, v in env_updates.items()},
                )

                if not env_updates:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "No env_updates provided"},
                    )
                else:
                    agent_id, service_id = TenantAgentPool.extract_ids(request)
                    apply_env_overrides_to_active(
                        env_updates,
                        service_id=service_id,
                        agent_id=agent_id,
                    )
                    for k, v in env_updates.items():
                        set_os_environ(
                            k, v, service_id=service_id, agent_id=agent_id
                        )
                    tip = effective_tip(service_id, agent_id)
                    model_name = str(tip.get("MODEL_NAME") or "unknown")
                    logger.info("[command.model] tip 已更新, MODEL_NAME=%s", model_name)

                    try:
                        from jiuwenclaw.agentserver.memory.config import clear_config_cache
                        clear_config_cache()
                        logger.info("[command.model] config cache 已清除")
                    except Exception as e:
                        logger.debug("[command.model] clear_config_cache skipped: %s", e)

                    try:
                        await self._agent_manager.reload_tenant_config(
                            agent_id,
                            service_id,
                            None,
                            env_updates,
                        )
                        logger.info("[command.model] agent config 已重载")
                    except Exception as e:
                        logger.debug("[command.model] reload_tenant_config skipped: %s", e)

                    current = str(
                        effective_tip(service_id, agent_id).get("MODEL_NAME") or "unknown"
                    )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "current": current,
                            "requested": target,
                            "type": "switched",
                            "applied": True,
                        },
                    )
                    logger.info("[command.model] 切换完成: current=%s", current)

            else:
                agent_id, service_id = TenantAgentPool.extract_ids(request)
                current = str(
                    effective_tip(service_id, agent_id).get("MODEL_NAME")
                    or get_local_config("MODEL_NAME", "unknown")
                    or "unknown"
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"current": current, "available": ["default-model"]},
                )

        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.model failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_resume(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            query = params.get("query")
            session_id = query if isinstance(query, str) and query.strip() else "sess_mock_resume"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "query": query if isinstance(query, str) else "",
                    "resumed": True,
                    "preview": "Mock resumed conversation",
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.resume failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_session(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "sess_mock"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "remote_url": f"https://example.com/session/{session_id}",
                    "qr_text": f"session:{session_id}",
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.session failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_browser_start(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """启动浏览器并返回执行结果（returncode）。"""
        try:
            from jiuwenclaw.agentserver.tools.browser_start_client import start_browser

            config_path = str(get_config_file())
            returncode = start_browser(dry_run=False, config_file=config_path)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"returncode": returncode},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] browser.start failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_browser_runtime_restart(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            from jiuwenclaw.agentserver.tools.browser_tools import (
                restart_local_browser_runtime_server,
            )

            agent_id, service_id = TenantAgentPool.extract_ids(request)
            result = restart_local_browser_runtime_server(
                service_id=service_id,
                agent_id=agent_id,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"result": result},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] browser.runtime_restart failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_config_cache_clear(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            from jiuwenclaw.agentserver.memory.config import clear_config_cache

            clear_config_cache()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"cleared": True},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] config.cache_clear failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_agent_reload_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        reload_trace_id = request.request_id
        try:
            params = request.params or {}
            config_payload = params.get("config")
            env_overrides = params.get("env")

            # 触发 AGENT_RELOAD_CONFIG hook
            try:
                from jiuwenclaw.extensions.registry import ExtensionRegistry
                ctx = AgentReloadConfigHookContext(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    config=config_payload,
                    env=env_overrides,
                )
                await ExtensionRegistry.get_instance().trigger(
                    AgentServerHookEvents.AGENT_RELOAD_CONFIG, ctx
                )
                # 允许扩展修改配置
                config_payload = ctx.config
                env_overrides = ctx.env
            except RuntimeError:
                # ExtensionRegistry 未初始化，跳过
                pass

            from jiuwenclaw.agentserver.reload_result import (
                log_agent_config_hot_reload,
                summarize_reload_payload,
            )
            if request.channel_id == "officeclaw":
                guard = TenantAgentPool.require_officeclaw_agent(request)
                if guard is not None:
                    resp = guard
                    wire = encode_agent_response_for_wire(
                        resp, response_id=request.request_id
                    )
                    async with send_lock:
                        await ws.send(json.dumps(wire, ensure_ascii=False))
                    return

            raw_agent = getattr(request, "agent_id", None)
            agent_id, service_id = TenantAgentPool.extract_ids(request)

            if (
                request.channel_id == "officeclaw"
                or (raw_agent is not None and str(raw_agent).strip())
            ):
                aggregate = await self._agent_manager.reload_tenant_config(
                    agent_id,
                    service_id,
                    config=config_payload,
                    env=env_overrides,
                    reload_trace_id=reload_trace_id,
                )
            else:
                aggregate = await self._agent_manager.reload_agents_config(
                    config=config_payload,
                    env=env_overrides,
                    reload_trace_id=reload_trace_id,
                )
            payload = aggregate.to_payload()
            log_agent_config_hot_reload(
                logger,
                reload_trace_id=reload_trace_id,
                phase="completed",
                source="AgentWebSocketServer",
                **summarize_reload_payload(payload),
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=not aggregate.failed or aggregate.applied > 0 or aggregate.deferred > 0,
                payload=payload,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] agent.reload_config failed: %s", e)
            from jiuwenclaw.agentserver.reload_result import (
                log_agent_config_hot_reload,
                redact_reload_error_message,
            )
            log_agent_config_hot_reload(
                logger,
                reload_trace_id=reload_trace_id,
                phase="failed",
                source="AgentWebSocketServer",
                level=logging.ERROR,
                error=redact_reload_error_message(str(e)),
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_sync_agents_configs(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Apply tenant catalog sync and/or relay team materialization.

        Relay often sends **one** ``sync_agents_configs`` with both:
        - ``service_id`` + ``revision`` + ``agents[]`` + ``shared_env`` (tenant catalog)
        - optional ``teams`` (modes.team + member ``.md`` materialization)

        These must be **composed**, not mutually exclusive: skipping the tenant
        path when ``teams`` is present drops env/catalog hot-reload.
        """
        from jiuwenclaw.schema.message import EventType

        params = request.params if isinstance(request.params, dict) else {}
        teams_payload = params.get("teams")
        agents_payload = params.get("agents")
        run_tenant = bool(params.get("service_id"))
        # Tenant ``agents[]`` are catalog specs; only materialize them as ``.md``
        # when there is no tenant ``service_id`` (standalone expert path).
        run_team_materialize = teams_payload is not None
        run_standalone_md = (not run_tenant) and agents_payload is not None

        if not run_tenant and not run_team_materialize and not run_standalone_md:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "event_type": EventType.SYNC_AGENTS_CONFIGS_RESULT.value,
                    "revision": params.get("revision"),
                    "teams_applied": False,
                    "agents_applied": False,
                },
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))
            return

        merged: dict[str, Any] = {
            "event_type": EventType.SYNC_AGENTS_CONFIGS_RESULT.value,
            "revision": params.get("revision"),
        }
        ok = True
        error: str | None = None
        error_code: str | None = None

        if run_tenant:
            try:
                tenant_payload = await self._agent_manager.sync_agents_configs(params)
                if isinstance(tenant_payload, dict):
                    merged.update(tenant_payload)
                    agents = tenant_payload.get("agents")
                    tenant_ok = (
                        isinstance(agents, list)
                        and all(
                            isinstance(item, dict) and item.get("ok") for item in agents
                        )
                    )
                    if not tenant_ok:
                        ok = False
                        error = "tenant sync_agents_configs reported agent failures"
                else:
                    ok = False
                    error = "tenant sync_agents_configs returned invalid payload"
            except ValueError as exc:
                ok = False
                error = str(exc)
                logger.warning(
                    "[AgentWebSocketServer] tenant sync_agents_configs rejected: %s",
                    exc,
                )
            except Exception as exc:
                ok = False
                error = str(exc)
                logger.exception(
                    "[AgentWebSocketServer] tenant sync_agents_configs failed: %s",
                    exc,
                )

        if ok and (run_team_materialize or run_standalone_md):
            try:
                team_names: list[str] = []
                materialized_names: list[str] = []
                if run_team_materialize:
                    if not isinstance(teams_payload, dict):
                        raise ValueError("teams must be an object")
                    team_names, materialized_names = await self._apply_synced_team_config(
                        teams_payload
                    )
                if run_standalone_md:
                    if not isinstance(agents_payload, list):
                        raise ValueError("agents must be an array")
                    materialized_names = (
                        materialized_names
                        + await self._apply_synced_standalone_agents(agents_payload)
                    )
                merged.update(
                    {
                        "team_names": team_names,
                        "agent_names": materialized_names,
                        "teams_applied": run_team_materialize,
                        "agents_applied": run_standalone_md,
                        "applies_to": "next_session",
                    }
                )
            except ValueError as exc:
                ok = False
                error = str(exc)
                error_code = "TEAM_CONFIG_INVALID"
                logger.warning(
                    "[AgentWebSocketServer] relay team/agent materialize rejected: %s",
                    exc,
                )
            except Exception as exc:
                ok = False
                error = str(exc)
                error_code = "TEAM_CONFIG_SYNC_FAILED"
                logger.exception(
                    "[AgentWebSocketServer] relay team/agent materialize failed: %s",
                    exc,
                )
        elif run_team_materialize or run_standalone_md:
            # Tenant already failed; still record that team materialize was skipped.
            merged.setdefault("teams_applied", False)
            merged.setdefault("agents_applied", False)

        if not ok:
            merged["error"] = error or "sync_agents_configs failed"
            if error_code:
                merged["code"] = error_code

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=ok,
            payload=merged,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_team_catalog_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Return preset teams from ``modes.team`` for relay/frontend catalog."""
        channel_id = request.channel_id or "web"
        try:
            teams = await asyncio.to_thread(self._read_team_catalog_entries)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[team.catalog.list] failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": f"team.catalog.list failed: {exc}"},
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=True,
                payload={"teams": teams},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    @staticmethod
    def _read_team_catalog_entries() -> list[dict[str, Any]]:
        """读取 modes.team 字典，组装成 relay 期望的预置团摘要列表。"""
        from jiuwenclaw.config import get_config

        config = get_config()
        if not isinstance(config, dict):
            return []
        modes = config.get("modes")
        if not isinstance(modes, dict):
            return []
        team_dict = modes.get("team")
        if not isinstance(team_dict, dict):
            return []

        def _summarize_member(m: Any) -> dict[str, Any]:
            if not isinstance(m, dict):
                return {}
            member_name = m.get("member_name") or m.get("name") or ""
            entry: dict[str, Any] = {"member_name": str(member_name)}
            if m.get("display_name"):
                entry["display_name"] = str(m["display_name"])
            if m.get("role_type"):
                entry["role_type"] = str(m["role_type"])
            return entry

        entries: list[dict[str, Any]] = []
        for team_name, spec in team_dict.items():
            if not isinstance(spec, dict):
                continue
            leader = spec.get("leader") if isinstance(spec.get("leader"), dict) else None
            members_raw = spec.get("predefined_members")
            predefined = (
                [_summarize_member(m) for m in members_raw if isinstance(m, dict)]
                if isinstance(members_raw, list)
                else []
            )
            display_name = (
                spec.get("display_name")
                or (leader.get("display_name") if leader else None)
                or team_name
            )
            entries.append({
                "team_name": str(spec.get("team_name") or team_name),
                "display_name": str(display_name) if display_name else None,
                "leader": _summarize_member(leader) if leader else None,
                "predefined_members": predefined,
            })
        return entries

    async def _reload_after_agents_sync(self) -> None:
        """Reload agent/team runtimes after relay materialization (no reload_scopes)."""
        from jiuwenclaw.config import get_config

        # Broadcast reload: AgentManager and TenantAgentPool both expose
        # reload_agents_config(config, env). Do NOT call reload_tenant_config here —
        # that API requires (agent_id, service_id, config, env).
        config_base = get_config()
        await self._agent_manager.reload_agents_config(config_base, None)

    async def _apply_synced_team_config(
        self,
        teams_payload: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        """Persist Relay Team/Agent config as one compensating transaction."""
        teams_payload = _normalize_relay_team_payload(teams_payload)
        from jiuwenclaw.agentserver.agent_config_service import (
            BUILTIN_AGENTS,
            AgentConfigService,
            build_team_member_agent_params,
        )
        from jiuwenclaw.agentserver.team.config_loader import get_team_template_snapshot
        from jiuwenclaw.config import (
            get_config,
            replace_teams_in_config,
            batch_upsert_subagents_in_config,
        )

        agent_params = build_team_member_agent_params(teams_payload)
        builtin_names = {agent.name for agent in BUILTIN_AGENTS}
        conflicting_builtin = next(
            (item.name for item in agent_params if item.name in builtin_names),
            None,
        )
        if conflicting_builtin:
            raise ValueError(f"不能覆盖内置 agent: {conflicting_builtin}")

        agent_service = AgentConfigService()
        team_names = [
            str(item.get("team_name") or "").strip()
            for item in teams_payload.get("team", [])
            if isinstance(item, dict)
        ]
        configured_team_names = {name for name in team_names if name}

        async with self._agents_sync_config_lock:
            config_path = Path(get_config_file())
            file_snapshots: dict[Path, bytes | None] = {
                config_path: _read_file_snapshot(config_path),
            }
            for item in agent_params:
                path = agent_service.get_agent_file_path(item.name, item.location)
                file_snapshots[path] = _read_file_snapshot(path)

            bindings = []
            entity_store = None
            try:
                from jiuwenclaw.agentserver.team_binding_store import get_team_binding_store
                from jiuwenclaw.agentserver.team_entity_store import get_team_entity_store

                binding_store = get_team_binding_store()
                entity_store = get_team_entity_store()
                bindings = binding_store.list()
                for binding in bindings:
                    entity_path = entity_store.entity_path(binding.team_name)
                    file_snapshots[entity_path] = _read_file_snapshot(entity_path)
            except Exception as store_exc:  # noqa: BLE001
                logger.debug(
                    "[AgentWebSocketServer] team binding/entity store unavailable: %s",
                    store_exc,
                )

            try:
                replace_teams_in_config(teams_payload)

                materialized_agents = [
                    agent_service.create_agent(item)
                    for item in agent_params
                ]
                if materialized_agents:
                    batch_upsert_subagents_in_config(
                        [agent.name for agent in materialized_agents],
                        enabled=True,
                    )

                config_base = get_config()
                if entity_store is not None:
                    for binding in bindings:
                        if binding.template_id not in configured_team_names:
                            continue
                        entity_store.write(
                            team_name=binding.team_name,
                            template_id=binding.template_id,
                            template_snapshot=get_team_template_snapshot(
                                config_base,
                                template_id=binding.template_id,
                            ),
                            created_at=binding.created_at,
                        )

                await self._reload_after_agents_sync()
            except Exception:
                for path, snapshot in file_snapshots.items():
                    try:
                        _restore_file_snapshot(path, snapshot)
                    except Exception as rollback_exc:  # noqa: BLE001
                        logger.exception(
                            "[AgentWebSocketServer] teams sync rollback failed: path=%s error=%s",
                            path,
                            rollback_exc,
                        )
                try:
                    await self._reload_after_agents_sync()
                except Exception as reload_exc:  # noqa: BLE001
                    logger.exception(
                        "[AgentWebSocketServer] teams sync rollback reload failed: %s",
                        reload_exc,
                    )
                raise

        return team_names, [agent.name for agent in materialized_agents]

    async def _apply_synced_standalone_agents(
        self,
        agents_payload: list[dict[str, Any]],
    ) -> list[str]:
        """Persist relay sync ``agents[]`` as standalone user ``.md`` experts."""
        from jiuwenclaw.agentserver.agent_config_service import (
            BUILTIN_AGENTS,
            AgentConfigService,
            build_single_agent_params,
        )
        from jiuwenclaw.config import batch_upsert_subagents_in_config

        agent_params = build_single_agent_params(agents_payload)
        builtin_names = {agent.name for agent in BUILTIN_AGENTS}
        conflicting_builtin = next(
            (item.name for item in agent_params if item.name in builtin_names),
            None,
        )
        if conflicting_builtin:
            raise ValueError(f"不能覆盖内置 agent: {conflicting_builtin}")

        agent_service = AgentConfigService()
        async with self._agents_sync_config_lock:
            file_snapshots: dict[Path, bytes | None] = {}
            for item in agent_params:
                path = agent_service.get_agent_file_path(item.name, item.location)
                file_snapshots[path] = _read_file_snapshot(path)

            try:
                materialized_agents = [
                    agent_service.create_agent(item) for item in agent_params
                ]
                if materialized_agents:
                    batch_upsert_subagents_in_config(
                        [agent.name for agent in materialized_agents],
                        enabled=True,
                    )

                await self._reload_after_agents_sync()
            except Exception:
                for path, snapshot in file_snapshots.items():
                    try:
                        _restore_file_snapshot(path, snapshot)
                    except Exception as rollback_exc:  # noqa: BLE001
                        logger.exception(
                            "[AgentWebSocketServer] standalone agents sync rollback failed: path=%s error=%s",
                            path,
                            rollback_exc,
                        )
                try:
                    await self._reload_after_agents_sync()
                except Exception as reload_exc:  # noqa: BLE001
                    logger.exception(
                        "[AgentWebSocketServer] standalone agents sync rollback reload failed: %s",
                        reload_exc,
                    )
                raise

        return [agent.name for agent in materialized_agents]

    async def _handle_extensions_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """获取所有 Rail 扩展列表."""
        try:
            manager = get_rail_manager(RuntimeScopeKey.from_request(request))
            extensions = manager.list_extensions()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"extensions": extensions},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.list failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_extensions_import(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """导入新的 Rail 扩展（文件夹结构）."""
        try:
            params = request.params or {}
            folder_path = params.get("folder_path")

            if not folder_path:
                raise ValueError("缺少 folder_path 参数")

            source_path = Path(folder_path)
            if not source_path.exists() or not source_path.is_dir():
                raise ValueError(f"文件夹不存在或不是目录: {folder_path}")

            manager = get_rail_manager(RuntimeScopeKey.from_request(request))
            extension = manager.import_extension(folder_path)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=extension,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.import failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_extensions_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """删除 Rail 扩展."""
        try:
            params = request.params or {}
            name = params.get("name")

            if not name:
                raise ValueError("缺少 name 参数")

            manager = get_rail_manager(RuntimeScopeKey.from_request(request))
            manager.delete_extension(name)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"deleted": True, "name": name},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.delete failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_extensions_toggle(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """切换 Rail 扩展的启用状态，并触发热更新."""
        try:
            params = request.params or {}
            name = params.get("name")
            enabled = params.get("enabled", False)

            if name is None:
                raise ValueError("缺少 name 参数")
            if enabled is None:
                raise ValueError("缺少 enabled 参数")

            manager = get_rail_manager(RuntimeScopeKey.from_request(request))

            # 1. 确保 agent 实例已设置（用于热更新）
            agent = self._agent_manager.get_agent_nowait()
            if agent is not None:
                agent_instance = agent.get_instance()
                if agent_instance is not None:
                    manager.set_agent_instance(agent_instance)

            # 2. 更新配置文件中的启用状态
            extension = manager.toggle_extension(name, enabled)

            # 3. 触发热更新：根据 enabled 状态注册或注销 rail
            await manager.hot_reload_rail(name, enabled)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=extension,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.toggle failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def send_push(self, msg) -> None:
        """AgentServer 主动向 Gateway 推送消息。

        payload 格式与 AgentResponse.payload 一致，
        可含 event_type 等字段供 Gateway 转为 Message 派发到 Channel。
        """
        if self._current_ws is None or self._current_send_lock is None:
            logger.warning(
                "[AgentWebSocketServer] send_push 失败: 无活跃 Gateway 连接"
            )
            return

        try:
            wire = build_server_push_wire(msg)
            async with self._current_send_lock:
                await self._current_ws.send(json.dumps(wire, ensure_ascii=False))
            response_kind = str(msg.get("response_kind") or "").strip()
            if response_kind:
                logger.info(
                    "[AgentWebSocketServer] send_push response_kind wire sent: channel_id=%s kind=%s",
                    msg.get("channel_id", ""),
                    response_kind,
                )
            else:
                logger.info(
                    "[AgentWebSocketServer] send_push 已发送(E2A wire): channel_id=%s",
                    msg.get("channel_id", ""),
                )
        except Exception as e:
            logger.warning("[AgentWebSocketServer] send_push 失败: %s", e)

    def get_agent(self):
        """获取 default agent 实例（向后兼容）."""
        return self._agent_manager.get_agent_nowait()

    def get_agent_manager(self) -> TenantAgentPool:
        """获取 AgentManager 实例."""
        return self._agent_manager

    # =========================================================================
    # 文件传输处理
    # =========================================================================

    async def _handle_file_transfer(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理文件传输请求（Gateway -> AgentServer）.

        支持：
        - file.transfer.start: 开始传输
        - file.transfer.chunk: 传输分片
        - file.transfer.complete: 完成传输
        """
        params = request.params if isinstance(request.params, dict) else {}
        event_type = params.get("event_type", "")
        transfer_id = params.get("transfer_id", "")

        ft_manager = get_file_transfer_manager()

        # 检查是否启用分布式模式
        if not ft_manager.enabled:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "file transfer not enabled (distributed mode required)",
                    "event_type": event_type,
                },
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))
            return

        try:
            if event_type == FILE_TRANSFER_START:
                ft_params = FileTransferStartParams(
                    transfer_id=transfer_id,
                    filename=params.get("filename", "unnamed"),
                    file_size=params.get("file_size", 0),
                    sha256=params.get("sha256", ""),
                    total_chunks=params.get("total_chunks", 0),
                    chunk_size=params.get("chunk_size", 65536),
                    mime_type=params.get("mime_type", ""),
                    session_id=request.session_id or "",
                    service_id=str(
                        params.get("service_id")
                        or getattr(request, "service_id", None)
                        or ""
                    ),
                    agent_id=str(
                        params.get("agent_id")
                        or getattr(request, "agent_id", None)
                        or ""
                    ),
                )
                result = await ft_manager.handle_transfer_start(ft_params)
            elif event_type == FILE_TRANSFER_CHUNK:
                result = await ft_manager.handle_transfer_chunk(
                    transfer_id=transfer_id,
                    chunk_index=params.get("chunk_index", 0),
                    base64_data=params.get("base64_data", ""),
                )
            elif event_type == FILE_TRANSFER_COMPLETE:
                result = await ft_manager.handle_transfer_complete(
                    transfer_id=transfer_id,
                    sha256=params.get("sha256", ""),
                )
            else:
                result = {
                    "accepted": False,
                    "error": f"unknown event_type: {event_type}",
                }

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=result.get("success", result.get("accepted", False)),
                payload={
                    "event_type": event_type,
                    **result,
                },
            )
        except Exception as e:
            logger.exception(
                "[AgentWebSocketServer] 文件传输处理失败: %s",
                e,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(e),
                    "event_type": event_type,
                },
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    @staticmethod
    def get_conversation_history(
        session_id: str,
        page_idx: int,
        *,
        sessions_root: str | Path | None = None,
    ) -> dict[str, Any] | None:
        # 按照 session_id 和分页消息获取历史记录
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        if not isinstance(page_idx, int) or page_idx <= 0:
            return None

        root = Path(sessions_root) if sessions_root else get_agent_sessions_dir()
        history_path: Path = root / session_id.strip() / "history.json"
        if not history_path.exists():
            return None
        try:
            raw = read_history_records_for_frontend(history_path)
        except Exception as e:
            logger.warning("Failed to read history for session %s: %s", session_id, e)
            return None
        if not isinstance(raw, list):
            return None

        page_size = 50
        total = len(raw)
        total_pages = max(1, math.ceil(total / page_size))
        if page_idx > total_pages:
            return None

        ordered = list(reversed(raw))
        start = (page_idx - 1) * page_size
        end = start + page_size
        resolved_sid = session_id.strip()
        page_slice = ordered[start:end]
        messages_out = enrich_history_messages_session_id(page_slice, resolved_sid)
        return {
            "messages": messages_out,
            "total_pages": total_pages,
            "page_idx": page_idx,
        }

    async def _handle_initialize(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 initialize 方法（非流式）.

        调用 AgentManager.initialize 完成初始化，返回 capabilities。

        Args:
            ws: WebSocket 连接
            request: AgentRequest
            send_lock: 发送锁
        """
        logger.info("[AgentServer] initialize: request_id=%s channel_id=%s", request.request_id, request.channel_id)

        try:
            params = request.params if isinstance(request.params, dict) else {}
            client_capabilities = params.get("clientCapabilities", {})
            logger.info(
                "[AgentServer] initialize clientCapabilities: %s",
                client_capabilities,
            )

            extra_config = {
                "protocol_version": params.get("protocolVersion", "0.1.0"),
                "client_capabilities": client_capabilities,
            }
            if request.channel_id == "acp":
                self._set_ws_acp_client_capabilities(ws, client_capabilities)

            channel_id = request.channel_id or "default"
            capabilities = await self._agent_manager.initialize(
                channel_id=channel_id,
                extra_config=extra_config,
            )
            if capabilities is None:
                capabilities = ACP_DEFAULT_CAPABILITIES.copy()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=capabilities,
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

            logger.info("[AgentServer] initialize completed: capabilities=%s", capabilities)

        except Exception as e:
            logger.exception("[AgentServer] initialize failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_session_create(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 session.create 方法.

        调用 AgentManager.create_session 创建会话，返回 session_id。

        Args:
            ws: WebSocket 连接
            request: AgentRequest
            send_lock: 发送锁
        """
        logger.info("[AgentServer] session.create: request_id=%s", request.request_id)

        try:
            channel_id = request.channel_id or "default"
            params = request.params if isinstance(request.params, dict) else {}
            explicit_session_id = params.get("session_id")
            session_id = await self._agent_manager.create_session(
                channel_id=channel_id,
                session_id=str(explicit_session_id).strip() if isinstance(explicit_session_id, str) else None,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=build_acp_session_new_result(session_id),
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

            logger.info("[AgentServer] session.create completed: session_id=%s", session_id)

        except Exception as e:
            logger.exception("[AgentServer] session.create failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_session_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 session.delete 请求：删除 Agent 本机 sessions 目录下的会话目录。"""
        import shutil

        logger.info("[AgentServer] session.delete: request_id=%s", request.request_id)

        try:
            params = request.params if isinstance(request.params, dict) else {}
            raw_sid = str(params.get("session_id") or "").strip()
            if not raw_sid:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                )
            else:
                safe_sid = normalize_safe_session_id(raw_sid)
                if safe_sid is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "invalid session_id", "code": "BAD_REQUEST"},
                    )
                else:
                    workspace_session_dir = _sessions_dir_for_request(request)
                    session_dir = resolve_session_dir_under_root(workspace_session_dir, safe_sid)
                    if session_dir is None:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "invalid session_id", "code": "BAD_REQUEST"},
                        )
                    elif not session_dir.exists():
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "session not found", "code": "NOT_FOUND"},
                        )
                    elif not session_dir.is_dir():
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "session is not a directory", "code": "BAD_REQUEST"},
                        )
                    else:
                        await asyncio.to_thread(shutil.rmtree, session_dir)
                        # 清理内存中的 agent 实例和缓存绑定
                        try:
                            agent_id, service_id = TenantAgentPool.extract_ids(request)
                            agent_manager = self._agent_manager.get_agent_manager_nowait(agent_id, service_id)
                            if agent_manager is not None:
                                channel_id = request.channel_id or "default"
                                await agent_manager.cleanup_all_modes(channel_id, safe_sid)
                        except Exception as cleanup_exc:
                            logger.warning("[AgentServer] session.delete memory cleanup failed: %s", cleanup_exc)
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload={"session_id": safe_sid},
                        )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentServer] session.delete failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_team_runtime_dissolve(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 team.runtime.dissolve：保上下文解散团队 runtime。

        供 relay 在团队配置修改后的下一条消息前调用：停旧 runtime、删静态表、
        标 db_state=cleaned，保留 checkpoint 对话记忆与 per-session 动态表，
        使下一轮 dispatch 落 CREATE 用新 spec 重建。

        幂等：session 无团队 runtime 可解散时返回 ok=True 且 dissolved=False，
        便于 relay 无条件调用。
        """
        logger.info(
            "[AgentServer] team.runtime.dissolve: request_id=%s",
            request.request_id,
        )

        try:
            params = request.params if isinstance(request.params, dict) else {}
            raw_sid = str(params.get("session_id") or "").strip()
            team_name = str(params.get("team_name") or "").strip() or None
            if not raw_sid:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                )
            else:
                safe_sid = normalize_safe_session_id(raw_sid)
                if safe_sid is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "invalid session_id", "code": "BAD_REQUEST"},
                    )
                else:
                    try:
                        result = await get_team_manager(
                            request.channel_id
                        ).dissolve_session_runtime_keep_context(
                            safe_sid,
                            team_name=team_name,
                        )
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload=result,
                        )
                    except TeamDissolveNameMismatchError as e:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": str(e), "code": "BAD_REQUEST"},
                        )
                    except TeamDissolveConflictError as e:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": str(e), "code": "CONFLICT"},
                        )
                    except TeamDissolveUnsupportedError as e:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": str(e), "code": "UNSUPPORTED_MODE"},
                        )
                    except TeamDissolveError as e:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": str(e), "code": "BAD_REQUEST"},
                        )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentServer] team.runtime.dissolve failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e), "code": "INTERNAL"},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_acp_tool_response(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        jsonrpc_id = params.get("jsonrpc_id")
        response_payload = params.get("response")
        if not isinstance(response_payload, dict):
            response_payload = {}

        if get_acp_output_manager().complete_jsonrpc_response(jsonrpc_id, response_payload):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"accepted": True},
            )
        else:
            logger.info(
                "[AgentServer] ignore unknown/late acp tool response: jsonrpc_id=%s request_id=%s",
                jsonrpc_id,
                request.request_id,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "accepted": False,
                    "ignored": True,
                    "reason": "unknown_or_late_response",
                    "jsonrpc_id": jsonrpc_id,
                },
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_client_tool_response(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        accepted, reason = get_client_tool_manager().complete(
            params,
            session_id=request.session_id,
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "accepted": accepted,
                "ignored": not accepted,
                "reason": reason,
                "tool_call_id": params.get("tool_call_id"),
            },
        )
        if not accepted:
            logger.info(
                "[AgentServer] ignore client tool response: tool_call_id=%s reason=%s",
                params.get("tool_call_id"),
                reason,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def handle_acp_tool_response_for_test(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Public test helper that delegates to ACP tool-response handling."""
        await self._handle_acp_tool_response(ws, request, send_lock)

    def is_working(self) -> bool:
        """返回 Agent 是否正在工作.

        用于沙箱保活校验。

        Returns:
            bool: 是否正在工作
        """
        return self._agent_manager.is_working()
