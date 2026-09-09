# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Coroutine

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    BUILTIN_AGENT_TYPE,
    AgentCreateFailed,
    AgentCreatingTimeout,
    AgentDeleted,
    AgentManager,
    AgentRuntime,
    is_third_party_agent_type,
)
from jiuwenswarm.extensions.agentos.agentos_router.agentos_authenticator import AgentOSAuthenticator
from jiuwenswarm.extensions.agentos.auth.common import (
    extract_headers,
    extract_token,
    get_remote_addr,
)
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthContext, AuthResult
from jiuwenswarm.extensions.agentos.agentos_router.config import (
    DEFAULT_AGENT_WORKSPACE_ROOT,
    SshChannelEndpoint,
)
from jiuwenswarm.extensions.agentos.agentos_router.logutil import (
    agentos_extra,
    format_agentos,
    log_agentos,
)
from jiuwenswarm.extensions.agentos.agentos_router.models import (
    AgentInfo,
    AgentStatus,
    ImageInfo,
)
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import (
    RegistryClient,
    instance_service_id,
)
from jiuwenswarm.extensions.agentos.agentos_router.ssh_relay import (
    DEFAULT_CLIENT_KEYS_DIR,
    YuanrongSshRelay,
    resolve_client_keys_dir,
)
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    AgentFileDownloadChunk,
    AgentRuntimeSpec,
    YuanrongAgentFileError,
    YuanrongFrontendAgentClient,
)
from jiuwenswarm.extensions.agentos.auth.ssh_key_issuer import SshKeyIssuer
from jiuwenswarm.gateway import ChannelManager
from jiuwenswarm.gateway.channel_manager.base import ChannelType
from jiuwenswarm.server.runtime.attachments.document_attachments import is_forbidden_document
from jiuwenswarm.gateway.routing.agent_client import (
    AgentServerClient,
    WebSocketAgentServerClient,
)
from jiuwenswarm.server.runtime.attachments.upload_storage import safe_upload_filename


logger = logging.getLogger(__name__)

_WORKSPACE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Builtin jiuwenswarm create only: sandbox env so agentserver can locate
# the host workspace bind path (``/home/agentos/users/<user_id>``).
USER_DIRECTORY_ENV_KEY = "JIUWENSWARM_USER_DIRECTORY"

# Gateway-side file transfer limits (design: stricter than YuanRong 512MB).
_AGENT_FILE_PATH_ROOT = "/home/agentos"
_MAX_AGENT_FILE_UPLOAD_BYTES = 50 * 1024 * 1024

_TEAM_MODES = frozenset({"team", "code.team", "team.plan"})
# Web/TUI 握手成功后预热内置沙箱；SSH/IM 等不走 agentserver WS，不预热。
_CONNECT_WARMUP_CHANNELS = frozenset(
    {ChannelType.WEB.value, ChannelType.CLI.value}
)

# create_sandbox 返回后 agentserver 仍在进程内启动；YuanRong WS 代理此时会回
# HTTP 502。在 deadline 内重试，避免首条 chat 立刻空失败（TUI "Worked for 0s"）。
_WS_CONNECT_READY_TIMEOUT_SECONDS = 60.0
_WS_CONNECT_RETRY_INTERVAL_SECONDS = 1.0
_WS_CONNECT_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})
_WS_CONNECT_RETRYABLE_TEXT_TOKENS = (
    "http 502",
    "http 503",
    "http 504",
    "connection refused",
    "temporarily unavailable",
)


def _should_log_ws_retry(attempt: int) -> bool:
    """Log retry WARNING sparsely: first attempt + every 10th attempt."""
    return attempt == 1 or attempt % 10 == 0


def _is_team_mode(params: Any) -> bool:
    """Return True if params["mode"] is a team variant."""
    if not isinstance(params, dict):
        return False
    return str(params.get("mode") or "").strip().lower() in _TEAM_MODES


def _is_ws_connect_retryable(exc: BaseException) -> bool:
    """冷启动期间 proxy/agentserver 未就绪的可重试错误."""
    status = getattr(exc, "status_code", None)
    if status in _WS_CONNECT_RETRYABLE_HTTP_STATUS:
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return True
    text = str(exc).lower()
    for token in _WS_CONNECT_RETRYABLE_TEXT_TOKENS:
        if token in text:
            return True
    return False


# AgentServer 请求阶段网络层错误的文本特征：WebSocketAgentServerClient 将
# 连接断开 / 收包失败 / unary 超时统一包装成 RuntimeError，只能按文本识别。
_AGENT_NETWORK_ERROR_TEXT_TOKENS = (
    "connection closed",
    "connection refused",
    "temporarily unavailable",
    "http 502",
    "http 503",
    "http 504",
    "unreachable",
    "请求超时",
    "connection reset",
)

# 断连清理的有限重试：断连后先等 disconnect_cleanup_timeout_seconds，再按固定
# 间隔最多重试几次，覆盖「chat 尾部 release / 延迟 cancel 的 interrupt 与
# 「断连+timeout」同秒到期」等短暂竞态；超限后交回 600s idle reaper 兜底。
_DISCONNECT_CLEANUP_RETRY_INTERVAL_SECONDS = 1.0
_DISCONNECT_CLEANUP_MAX_ATTEMPTS = 3
# pop_if_idle 的固定 idle 宽限（秒）：真正的安全守卫是连接数==0 + task_count==0
# + READY，宽限只需覆盖 release 的 touch 落地竞态，秒级即可。
_DISCONNECT_CLEANUP_IDLE_GRACE_SECONDS = 1.0


def _is_agent_network_error(exc: BaseException) -> bool:
    """True when *exc* indicates an AgentServer network-layer failure.

    覆盖两类场景：
    - 连接阶段：``_connect_ws_until_ready`` 重试耗尽后抛出的原始网络异常
      （ConnectionError / Timeout / OSError / 502 等）；
    - 请求阶段：WebSocketAgentServerClient 包装的 ``RuntimeError``（连接
      断开、非流式请求超时）。

    业务层错误（ValueError、duplicate request_id 等非网络 RuntimeError）
    返回 False，交由原有路径处理。
    """
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return True
    status = getattr(exc, "status_code", None)
    if status in _WS_CONNECT_RETRYABLE_HTTP_STATUS:
        return True
    text = str(exc).lower()
    for token in _AGENT_NETWORK_ERROR_TEXT_TOKENS:
        # 部分中文 token 在 lower() 后不受影响，直接子串匹配
        if token in text:
            return True
    return False


class UnsupportedAgentType(ValueError):
    pass


class EphemeralKeyIssueError(RuntimeError):
    """A configured issuer could not mint an ephemeral SSH key."""


class AgentOSFileTransferError(RuntimeError):
    """Raised when AgentOS container file transfer cannot be completed."""

    def __init__(self, message: str, *, code: str = "INTERNAL_ERROR") -> None:
        super().__init__(message)
        self.code = str(code)


# Upload: relative path (+ optional dir prefix) → ``/home/agentos/<relative>``.
# Download / list / mkdir: absolute path under ``/home/agentos``.


def _normalize_upload_dir_prefix(dir_prefix: str) -> str:
    """Return a clean relative directory prefix, or empty string for default root."""
    text = str(dir_prefix or "").strip().replace("\\", "/")
    if not text or text == ".":
        return ""
    if text.startswith("/"):
        raise AgentOSFileTransferError(
            "upload dir must be relative (AgentOS prefixes /home/agentos)",
            code="BAD_REQUEST",
        )
    text = text.strip("/")
    if not text or text == ".":
        return ""
    posix = PurePosixPath(text)
    if ".." in posix.parts or posix.is_absolute():
        raise AgentOSFileTransferError("path must not contain '..'", code="BAD_REQUEST")
    return posix.as_posix()


def normalize_agent_file_upload_path(
    path: str,
    *,
    dir_prefix: str = "",
    user_id: str = "",
) -> str:
    """Normalize upload path: relative (+ optional ``dir_prefix``) → ``/home/agentos/...``.

    - ``dir_prefix`` empty: land under ``/home/agentos`` (e.g. ``a.pdf`` → ``/home/agentos/a.pdf``)
    - ``dir_prefix`` set: prefix directory (e.g. ``docs`` + ``a.pdf`` → ``/home/agentos/docs/a.pdf``)
    """
    del user_id  # reserved for future per-user upload roots
    text = str(path or "").strip().replace("\\", "/")
    if not text or text in {".", "/"}:
        raise AgentOSFileTransferError("path is required", code="BAD_REQUEST")
    if text.startswith("/"):
        raise AgentOSFileTransferError(
            "upload path must be relative (AgentOS prefixes /home/agentos)",
            code="BAD_REQUEST",
        )
    posix = PurePosixPath(text)
    if ".." in posix.parts or posix.is_absolute():
        raise AgentOSFileTransferError("path must not contain '..'", code="BAD_REQUEST")

    raw_name = posix.name
    if not raw_name or raw_name in {".", ".."}:
        raise AgentOSFileTransferError("path is required", code="BAD_REQUEST")
    safe_name = safe_upload_filename(raw_name, fallback="upload.bin")
    if is_forbidden_document(filename=safe_name):
        raise AgentOSFileTransferError("forbidden extension", code="BAD_REQUEST")

    path_parent = posix.parent.as_posix().strip(".")
    prefix = _normalize_upload_dir_prefix(dir_prefix)
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if path_parent and path_parent != ".":
        parts.append(path_parent)
    parts.append(safe_name)
    return f"{_AGENT_FILE_PATH_ROOT}/{'/'.join(parts)}"


def normalize_agent_file_download_path(path: str) -> str:
    """Normalize download/list path: absolute and under ``/home/agentos``."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        raise AgentOSFileTransferError("path is required", code="BAD_REQUEST")
    if not text.startswith("/"):
        raise AgentOSFileTransferError("download path must be absolute", code="BAD_REQUEST")
    posix = PurePosixPath(text)
    if ".." in posix.parts:
        raise AgentOSFileTransferError("path must not contain '..'", code="BAD_REQUEST")
    root = _AGENT_FILE_PATH_ROOT
    if text != root and not text.startswith(f"{root}/"):
        raise AgentOSFileTransferError(
            f"path must be under {root}",
            code="BAD_REQUEST",
        )
    return text


def enforce_agent_file_upload_size(content: bytes) -> None:
    """Reject uploads larger than the Gateway-side 50MB limit."""
    size = len(content)
    if size > _MAX_AGENT_FILE_UPLOAD_BYTES:
        raise AgentOSFileTransferError(
            f"file size exceeds {_MAX_AGENT_FILE_UPLOAD_BYTES} bytes limit",
            code="file_too_large",
        )


def build_auth_headers_from_mapping(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Build Authorization header for YuanRong file APIs from WS/auth headers."""
    if not headers:
        return {}
    lowered = {str(k).lower(): str(v) for k, v in headers.items() if v is not None}
    auth = lowered.get("authorization", "").strip()
    if auth:
        return {"Authorization": auth}
    token = lowered.get("x-token", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def build_auth_headers_from_token(token: str | None) -> dict[str, str]:
    text = str(token or "").strip()
    if not text:
        return {}
    if text.lower().startswith("bearer "):
        return {"Authorization": text}
    return {"Authorization": f"Bearer {text}"}


def build_inline_runtime_spec(image_info: ImageInfo) -> AgentRuntimeSpec:
    """Take registry ``metadata.runtime_spec`` (YuanRong ``RuntimeSpec`` shape)."""
    meta = image_info.metadata if isinstance(image_info.metadata, dict) else {}
    raw_spec = meta.get("runtime_spec")
    if not isinstance(raw_spec, Mapping) or not raw_spec:
        raise ValueError(
            f"runtime_spec is required from registry for agent_type={image_info.image_name}"
        )
    return dict(raw_spec)  # type: ignore[return-value]


def resolve_agent_workspace(user_id: str, *, workspace_root: str | None = None) -> str:
    """Resolve host workspace bind path for one agent user.

    Default: ``/home/agentos/users/<user_id>``. Optional ``workspace_root``
    overrides the parent directory (``{workspace_root}/<user_id>``).

    The gateway does **not** create the directory. It only validates that the
    directory already exists and is a directory. The directory's owner/group
    and permission setup is validated by other management-plane components,
    so no permission check is performed here. Any validation failure raises
    :class:`ValueError`, which the caller turns into a failed request.
    """
    safe_user = _WORKSPACE_NAME_RE.sub("_", str(user_id or "").strip()) or "default"
    root = Path(workspace_root or DEFAULT_AGENT_WORKSPACE_ROOT).expanduser()
    workspace = (root / safe_user).resolve()
    _validate_agent_workspace(workspace)
    return str(workspace)


def _validate_agent_workspace(workspace: Path) -> None:
    """Raise :class:`ValueError` unless *workspace* exists and is a directory.

    Owner/group and permission validation is delegated to other management-plane
    components, so only existence and type are checked here.
    """
    if not workspace.exists():
        raise ValueError(
            f"agent workspace does not exist: {workspace} "
            "(create it before creating a sandbox)"
        )
    if not stat.S_ISDIR(workspace.stat().st_mode):
        raise ValueError(f"agent workspace is not a directory: {workspace}")


class AgentOSRouterClient(AgentServerClient):
    """AgentServerClient implementation backed by YuanRong and AgentManager."""

    def __init__(
        self,
        yuanrong: YuanrongFrontendAgentClient,
        registry: RegistryClient,
        agent_manager: AgentManager,
        ssh_relay: YuanrongSshRelay | None = None,
        ssh_channel_endpoint: SshChannelEndpoint | None = None,
        key_issuer: SshKeyIssuer | None = None,
        ephemeral_key_ttl_sec: float = 300.0,
        workspace_root: str | None = None,
        sandbox_idle_timeout_seconds: float = 600.0,
        sandbox_idle_check_interval_seconds: float = 30.0,
        disconnect_cleanup_timeout_seconds: float = 60.0,
        connect_warmup_enabled: bool = True,
        auth_client: AgentOSAuthenticator | None = None,
        ws_client_factory: Callable[[], WebSocketAgentServerClient] | None = None,
    ) -> None:
        self._yuanrong = yuanrong
        self._registry = registry
        self._agent_manager = agent_manager
        self._ssh_relay = ssh_relay
        self._ssh_channel_endpoint = ssh_channel_endpoint
        self._key_issuer = key_issuer
        self._ephemeral_key_ttl_sec = float(ephemeral_key_ttl_sec)
        self._workspace_root = (
            str(workspace_root or "").strip() or DEFAULT_AGENT_WORKSPACE_ROOT
        )
        # <= 0 disables idle sandbox reclamation entirely.
        self._sandbox_idle_timeout_seconds = float(sandbox_idle_timeout_seconds)
        self._sandbox_idle_check_interval_seconds = max(
            1.0, float(sandbox_idle_check_interval_seconds)
        )
        # <= 0 disables the channel-disconnect cleanup path entirely.
        self._disconnect_cleanup_timeout_seconds = float(
            disconnect_cleanup_timeout_seconds
        )
        self._connect_warmup_enabled = bool(connect_warmup_enabled)
        self._idle_reaper_task: asyncio.Task[None] | None = None
        self._server_ready = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        # 用户当前 agent_type（3rdagent.switch 成功后更新）；SSH 接入跟随此值。
        self._current_agent_types: dict[str, str] = {}
        self._auth_client = auth_client
        # 延迟清理任务：user_id → pending cleanup task
        self._pending_cleanups: dict[str, asyncio.Task[None]] = {}
        # Web/TUI 连接预热：user_id → in-flight warmup task（同用户合并）
        self._warmup_tasks: dict[str, asyncio.Task[None]] = {}
        # create 后走 YuanRong frontend 的 WS 代理直连 instance（不走 invoke 链路）：
        # instance_id(sandbox_id) → 已连接的 WebSocketAgentServerClient
        self._ws_clients: dict[str, WebSocketAgentServerClient] = {}
        self._ws_clients_lock = asyncio.Lock()
        # instance_id → 正在进行的 connect Future（合并并发首连，避免多路同时打 502）
        self._ws_connecting: dict[str, asyncio.Future[WebSocketAgentServerClient]] = {}
        self._ws_client_factory = ws_client_factory or WebSocketAgentServerClient
        self._push_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None


    def set_channel_manager(self, channel_manager: ChannelManager) -> None:
        """Subscribe Web/TUI connect hooks (token auth) and channel disconnect events."""
        web_channel = channel_manager.get_channel(ChannelType.WEB)
        tui_channel = channel_manager.get_channel(ChannelType.CLI)

        if web_channel:
            on_connect = getattr(web_channel, "on_connect", None)
            if callable(on_connect):
                on_connect(self.on_connect)
        if tui_channel:
            on_connect = getattr(tui_channel, "on_connect", None)
            if callable(on_connect):
                on_connect(self.on_connect)

        channel_manager.subscribe_channel_events(self._on_channel_event)

    @staticmethod
    def _ws_channel_name(ws: Any) -> str:
        path = str(getattr(ws, "path", "") or "")
        if "/tui" in path:
            return "tui"
        if "/ws" in path:
            return "web"
        return str(getattr(ws, "channel_id", "") or "")

    @staticmethod
    def _runtime_log_fields(runtime: AgentRuntime) -> dict[str, Any]:
        info = runtime.info
        sandbox_id = str(info.sandbox_id or "")
        return {
            "user_id": info.user_id,
            "session_id": str(info.metadata.get("session_id") or ""),
            "sandbox_id": sandbox_id,
            "agent_type": info.agent_type,
            "instance": sandbox_id,
        }

    @staticmethod
    def _envelope_log_fields(envelope: E2AEnvelope) -> dict[str, Any]:
        return {
            "user_id": str(envelope.user_id or ""),
            "session_id": str(envelope.session_id or ""),
            "request_id": str(envelope.request_id or ""),
            "channel": str(envelope.channel or ""),
            "method": str(envelope.method or ""),
        }

    def _log_route(
        self,
        event: str,
        envelope: E2AEnvelope,
        runtime: AgentRuntime | None = None,
        *,
        level: int = logging.INFO,
        error: str = "",
    ) -> None:
        fields = self._envelope_log_fields(envelope)
        if runtime is not None:
            fields.update(self._runtime_log_fields(runtime))
            fields["user_id"] = fields["user_id"] or str(runtime.info.user_id or "")
            fields["session_id"] = fields["session_id"] or str(
                runtime.info.metadata.get("session_id") or ""
            )
        if error:
            fields["error"] = error
        log_agentos(logger, level, event, **fields)

    async def on_connect(self, ws: Any) -> AuthResult | None:
        channel = self._ws_channel_name(ws)
        remote = get_remote_addr(ws)
        if self._auth_client is None:
            # auth 未启用时回落使用握手头里的 X-User-Id，
            # 否则 user_id 为空会跳过连接计数/延迟清理，导致 agent 泄漏不回收。
            headers = {k.lower(): v for k, v in extract_headers(ws).items()}
            fallback_user_id = str(headers.get("x-user-id", "") or "").strip()
            fields: dict[str, Any] = {
                "user_id": fallback_user_id,
                "channel": channel,
                "remote": remote,
            }
            if not fallback_user_id:
                fields["uid_empty"] = "yes"
            log_agentos(logger, logging.DEBUG, "auth.skip", **fields)
            return AuthResult(
                success=True,
                user_id=fallback_user_id,
            )
        token = extract_token(ws)
        headers = extract_headers(ws)
        context = AuthContext(
            channel_type="",
            credentials={"token": token} if token else {},
            headers=headers,
            remote_addr=remote,
        )
        result = await self._auth_client.authenticate(context)
        if result.success:
            log_agentos(
                logger,
                logging.INFO,
                "auth.ok",
                user_id=result.user_id,
                channel=channel,
                remote=remote,
            )
        else:
            error_code = ""
            if isinstance(result.extensions, dict):
                error_code = str(result.extensions.get("error_code") or "")
            log_agentos(
                logger,
                logging.WARNING,
                "auth.deny",
                user_id=result.user_id,
                channel=channel,
                remote=remote,
                error=error_code or result.error or "unauthorized",
            )
            close = getattr(ws, "close", None)
            if callable(close):
                ret = close(code=1008, reason="unauthorized")
                if hasattr(ret, "__await__"):
                    await ret
        return result

    def set_key_issuer(
        self,
        key_issuer: SshKeyIssuer | None,
        *,
        ephemeral_key_ttl_sec: float = 300.0,
    ) -> None:
        """Inject or clear the northbound SSH ephemeral key issuer."""
        self._key_issuer = key_issuer
        self._ephemeral_key_ttl_sec = float(ephemeral_key_ttl_sec)

    async def _on_channel_event(self, event: Any) -> None:
        """处理 Channel 连接事件：连接计数、延迟清理、Web/TUI 预热。"""
        user_id = str(getattr(event, "user_id", "") or "").strip()
        if not user_id:
            return
        event_type = str(getattr(event, "event_type", "") or "").strip()
        if event_type == "connected":
            self._agent_manager.increment_user_connections(user_id)
            # 取消可能挂起的延迟清理
            task = self._pending_cleanups.pop(user_id, None)
            if task is not None and not task.done():
                task.cancel()
            channel_type = str(getattr(event, "channel_type", "") or "").strip().lower()
            if channel_type in _CONNECT_WARMUP_CHANNELS:
                self._schedule_connect_warmup(user_id)
        elif event_type == "disconnected":
            count = self._agent_manager.decrement_user_connections(user_id)
            # 配置的超时清理时长为0或者负数时，不触发清理机制
            if count <= 0 and self._disconnect_cleanup_timeout_seconds > 0:
                # 连接数为 0：超时后尝试删除 jiuwenswarm agent
                task = asyncio.create_task(
                    self._delayed_cleanup(user_id),
                    name=f"agentos-delayed-cleanup-{user_id[:24]}",
                )
                self._pending_cleanups[user_id] = task
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def _delayed_cleanup(self, user_id: str) -> None:
        """连接断开后，若用户仍无连接且 agent 真的空闲，删除其 jiuwenswarm agent。

        和 :meth:`_reap_idle_once` 走同一条 pop_if_idle 路径（READY、
        task_count==0、空闲超过宽限），避免在 in-flight 的 chat/SSH 还未释放时
        强制 kill。超时时长由 ``disconnect_cleanup_timeout_seconds`` 配置，<= 0
        表示关闭该路径。

        断连后先等 ``timeout``，再以固定间隔做有限次尝试，每次尝试都重新检查连接数与空闲状态。
        由于此流程涉及到的计时竞态条件过于复杂，暂时不做每个场景的精准处理。
        超限放弃后交回 600s idle reaper 兜底。
        """
        timeout = self._disconnect_cleanup_timeout_seconds
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        for attempt in range(1, _DISCONNECT_CLEANUP_MAX_ATTEMPTS + 1):
            # 二次检查：用户可能已重连（重连时上层也会 cancel 本任务）
            if self._agent_manager.get_user_connection_count(user_id) > 0:
                return
            try:
                runtimes = await self._agent_manager.list_user_agents(user_id)
            except Exception:
                logger.exception(
                    "[AgentOSRouter] delayed cleanup list_user_agents failed: user=%s",
                    user_id,
                )
                return

            pending = False
            for runtime in runtimes:
                if runtime.info.agent_type != BUILTIN_AGENT_TYPE:
                    continue
                key_values: dict[str, Any] | None = None
                session_id = str(runtime.info.metadata.get("session_id") or "")
                sandbox_id = str(runtime.info.sandbox_id or "")
                if "session_id" in self._agent_manager.key_fields:
                    if session_id:
                        key_values = {"session_id": session_id}
                if runtime.task_count > 0:
                    # in-flight 请求持有（chat.interrupt 延迟 cancel 等）：
                    # 等它 release 后再评估，本轮先标记继续重试。
                    pending = True
                    continue
                try:
                    # pop_if_idle 做最终守卫：task_count>0 / 非 READY / 空闲
                    # 不满宽限时返回 False，不会误杀活动中的 agent。
                    deleted = await self.delete_agent(
                        user_id,
                        runtime.info.agent_type,
                        key_values=key_values,
                        idle_timeout_seconds=_DISCONNECT_CLEANUP_IDLE_GRACE_SECONDS,
                    )
                except Exception:
                    logger.exception(
                        "[AgentOSRouter] delayed cleanup delete failed: "
                        "user=%s agent_type=%s sandbox_id=%s",
                        user_id,
                        runtime.info.agent_type,
                        sandbox_id,
                    )
                    pending = True
                    continue
                if deleted:
                    logger.info(
                        "[AgentOSRouter] delayed cleanup deleted agent: "
                        "user=%s session_id=%s sandbox_id=%s agent_type=%s",
                        user_id,
                        session_id,
                        sandbox_id,
                        runtime.info.agent_type,
                    )
                else:
                    # pop_if_idle 仍拒绝（busy / 非 READY / idle 不足）：重试。
                    pending = True

            if not pending:
                return
            if attempt >= _DISCONNECT_CLEANUP_MAX_ATTEMPTS:
                logger.warning(
                    "[AgentOSRouter] delayed cleanup retry budget exhausted: "
                    "user=%s attempts=%s",
                    user_id,
                    attempt,
                )
                return
            logger.info(
                "[AgentOSRouter] delayed cleanup retrying: user=%s attempt=%s wait_s=%s",
                user_id,
                attempt,
                _DISCONNECT_CLEANUP_RETRY_INTERVAL_SECONDS,
            )
            try:
                await asyncio.sleep(_DISCONNECT_CLEANUP_RETRY_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

    def _schedule_connect_warmup(self, user_id: str) -> None:
        """Fire-and-forget builtin sandbox + instance WS warmup for one user."""
        if self._closed or not self._connect_warmup_enabled:
            return
        if "session_id" in self._agent_manager.key_fields:
            logger.info(
                "[AgentOSRouter] skip connect warmup: session_id in "
                "agent_key_fields user=%s",
                user_id,
            )
            return
        existing = self._warmup_tasks.get(user_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._warmup_user_agent(user_id),
            name=f"agentos-connect-warmup-{user_id[:24]}",
        )
        self._warmup_tasks[user_id] = task
        self._background_tasks.add(task)

        def _on_done(done: asyncio.Task[None]) -> None:
            self._background_tasks.discard(done)
            if self._warmup_tasks.get(user_id) is done:
                self._warmup_tasks.pop(user_id, None)

        task.add_done_callback(_on_done)

    async def _warmup_user_agent(self, user_id: str) -> None:
        """Create the builtin jiuwenswarm sandbox and open instance WS.

        Must not raise: handshake / connection.ack already succeeded. Chat
        still goes through get_or_create_agent, so a failed warmup only
        means the first request pays the cold-start cost.
        """
        if self._closed:
            return
        agent_type = BUILTIN_AGENT_TYPE
        logger.info(
            "[AgentOSRouter] connect warmup start: user=%s agent_type=%s",
            user_id,
            agent_type,
        )
        try:
            runtime = await self._agent_manager.get_or_create_agent(
                user_id,
                agent_type,
                creator=self._create_agent,
                acquire=False,
            )
            await self._get_ws_client(runtime)
        except Exception:
            logger.warning(
                "[AgentOSRouter] connect warmup failed: user=%s agent_type=%s",
                user_id,
                agent_type,
                exc_info=True,
            )
            return
        if self._closed:
            return
        logger.info(
            "[AgentOSRouter] connect warmup ready: user=%s agent_type=%s "
            "sandbox_id=%s",
            user_id,
            agent_type,
            runtime.info.sandbox_id,
        )

    def get_current_agent_type(self, user_id: str) -> str:
        """Return the user's current agent_type (default ``jiuwenswarm``)."""
        uid = str(user_id or "").strip()
        return self._current_agent_types.get(uid) or BUILTIN_AGENT_TYPE

    async def resolve_instance_id_for_files(
        self,
        *,
        user_id: str,
        agent_type: str | None = None,
        session_id: str = "",
        instance_id: str | None = None,
    ) -> str:
        """Resolve YuanRong instance id for container file upload/download."""
        resolved, _key = await self._resolve_file_runtime(
            user_id=user_id,
            agent_type=agent_type,
            session_id=session_id,
            instance_id=instance_id,
            acquire=False,
        )
        return resolved

    async def _resolve_file_runtime(
        self,
        *,
        user_id: str,
        agent_type: str | None = None,
        session_id: str = "",
        instance_id: str | None = None,
        acquire: bool = False,
    ) -> tuple[str, Any]:
        """Return ``(instance_id, runtime_key)``; ``runtime_key`` is set when acquired."""
        explicit = str(instance_id or "").strip()
        if explicit:
            return explicit, None

        uid = str(user_id or "").strip()
        if not uid:
            raise AgentOSFileTransferError("user_id is required", code="BAD_REQUEST")

        try:
            normalized_type = AgentRuntime.normalize_agent_type(
                agent_type or self.get_current_agent_type(uid)
            )
        except ValueError as exc:
            raise AgentOSFileTransferError(str(exc), code="BAD_REQUEST") from exc

        key_values: dict[str, Any] | None = None
        if session_id and "session_id" in self._agent_manager.key_fields:
            key_values = {"session_id": session_id}

        acquired_key = None
        # Builtin jiuwenswarm: same as chat.send — create sandbox then use instance files API.
        if self._uses_direct_yuanrong(normalized_type):
            try:
                runtime = await self._agent_manager.get_or_create_agent(
                    uid,
                    normalized_type,
                    key_values=key_values,
                    creator=self._create_agent,
                    metadata={"session_id": session_id} if session_id else None,
                    acquire=acquire,
                )
            except (ValueError, AgentCreatingTimeout, AgentCreateFailed, AgentDeleted) as exc:
                raise AgentOSFileTransferError(str(exc), code="INTERNAL_ERROR") from exc
            acquired_key = runtime.key if acquire else None
        else:
            # Third-party agents: resolve existing runtime only (no create here).
            # Still acquire when requested so idle reaper cannot reclaim mid-transfer.
            runtime = await self._agent_manager.get_agent(
                uid,
                normalized_type,
                key_values=key_values,
                acquire=acquire,
            )
            if runtime is None or not runtime.is_ready():
                raise AgentOSFileTransferError(
                    "instance not found or not running",
                    code="instance_not_found",
                )
            acquired_key = runtime.key if acquire else None

        resolved = str(runtime.info.sandbox_id or "").strip()
        if not resolved:
            if acquired_key is not None:
                await self._agent_manager.release(acquired_key)
            raise AgentOSFileTransferError(
                "agent has no yuanrong instance_id",
                code="instance_not_found",
            )
        return resolved, acquired_key

    async def upload_container_file(
        self,
        *,
        user_id: str,
        path: str,
        content: bytes,
        dir_prefix: str = "",
        agent_type: str | None = None,
        session_id: str = "",
        instance_id: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Upload bytes into the user's agent container workspace."""
        enforce_agent_file_upload_size(content)
        normalized_path = normalize_agent_file_upload_path(
            path, dir_prefix=dir_prefix, user_id=user_id
        )
        resolved_instance_id, runtime_key = await self._resolve_file_runtime(
            user_id=user_id,
            agent_type=agent_type,
            session_id=session_id,
            instance_id=instance_id,
            acquire=True,
        )
        try:
            return await self._yuanrong.upload_agent_file(
                resolved_instance_id,
                normalized_path,
                content,
                auth_headers=build_auth_headers_from_mapping(auth_headers),
            )
        except YuanrongAgentFileError as exc:
            raise AgentOSFileTransferError(str(exc), code=exc.error_code) from exc
        finally:
            if runtime_key is not None:
                await self._agent_manager.release(runtime_key)

    async def download_container_file(
        self,
        *,
        user_id: str,
        path: str,
        offset: int = 0,
        limit: int = 65536,
        agent_type: str | None = None,
        session_id: str = "",
        instance_id: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> AgentFileDownloadChunk:
        """Download one chunk from the user's agent container."""
        normalized_path = normalize_agent_file_download_path(path)
        resolved_instance_id, runtime_key = await self._resolve_file_runtime(
            user_id=user_id,
            agent_type=agent_type,
            session_id=session_id,
            instance_id=instance_id,
            acquire=True,
        )
        try:
            return await self._yuanrong.download_agent_file(
                resolved_instance_id,
                normalized_path,
                offset=max(int(offset), 0),
                limit=max(int(limit), 1),
                auth_headers=build_auth_headers_from_mapping(auth_headers),
            )
        except YuanrongAgentFileError as exc:
            raise AgentOSFileTransferError(str(exc), code=exc.error_code) from exc
        finally:
            if runtime_key is not None:
                await self._agent_manager.release(runtime_key)

    async def list_container_files(
        self,
        *,
        user_id: str,
        dir_path: str,
        recursive: bool = False,
        max_depth: int = 0,
        agent_type: str | None = None,
        session_id: str = "",
        instance_id: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """List files in a directory inside the user's agent container."""
        if int(max_depth) < 0:
            raise AgentOSFileTransferError("max_depth must be >= 0", code="BAD_REQUEST")
        normalized_dir = normalize_agent_file_download_path(dir_path)
        resolved_instance_id, runtime_key = await self._resolve_file_runtime(
            user_id=user_id,
            agent_type=agent_type,
            session_id=session_id,
            instance_id=instance_id,
            acquire=True,
        )
        try:
            return await self._yuanrong.list_agent_files(
                resolved_instance_id,
                normalized_dir,
                recursive=bool(recursive),
                max_depth=int(max_depth),
                auth_headers=build_auth_headers_from_mapping(auth_headers),
            )
        except YuanrongAgentFileError as exc:
            raise AgentOSFileTransferError(str(exc), code=exc.error_code) from exc
        finally:
            if runtime_key is not None:
                await self._agent_manager.release(runtime_key)

    async def mkdir_container_dir(
        self,
        *,
        user_id: str,
        path: str,
        mode: str | None = None,
        recursive: bool = False,
        agent_type: str | None = None,
        session_id: str = "",
        instance_id: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a directory inside the user's agent container."""
        normalized_path = normalize_agent_file_download_path(path)
        if "\x00" in normalized_path:
            raise AgentOSFileTransferError("path must not contain NUL", code="BAD_REQUEST")
        resolved_instance_id, runtime_key = await self._resolve_file_runtime(
            user_id=user_id,
            agent_type=agent_type,
            session_id=session_id,
            instance_id=instance_id,
            acquire=True,
        )
        try:
            return await self._yuanrong.mkdir_agent_dir(
                resolved_instance_id,
                normalized_path,
                mode=mode,
                recursive=bool(recursive),
                auth_headers=build_auth_headers_from_mapping(auth_headers),
            )
        except YuanrongAgentFileError as exc:
            raise AgentOSFileTransferError(str(exc), code=exc.error_code) from exc
        finally:
            if runtime_key is not None:
                await self._agent_manager.release(runtime_key)

    @staticmethod
    def _uses_direct_yuanrong(agent_type: str) -> bool:
        """Builtin swarm uses URN invoke (same as ``agent_client.type=yuanrong``)."""
        return str(agent_type or "").strip().lower() == BUILTIN_AGENT_TYPE

    @property
    def server_ready(self) -> bool:
        return self._server_ready and self._yuanrong.server_ready

    async def connect(self, uri: str) -> None:
        await self._yuanrong.connect(uri)
        self._closed = False
        self._server_ready = True
        self._ensure_idle_reaper_task()

    async def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server_ready = False
        await self._stop_idle_reaper_task()
        # 取消所有挂起的延迟清理任务
        for task in self._pending_cleanups.values():
            if not task.done():
                task.cancel()
        self._pending_cleanups.clear()
        self._warmup_tasks.clear()
        await self._drain_background_tasks()
        await self._close_all_ws_clients()
        try:
            await self._yuanrong.disconnect()
        finally:
            await self._registry.close()

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        self._yuanrong.set_or_update_server_config(config=config, env=env)

    def set_server_push_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        self._push_handler = handler
        setter = getattr(self._yuanrong, "set_server_push_handler", None)
        if callable(setter):
            setter(handler)
        for ws_client in self._ws_clients.values():
            ws_client.set_server_push_handler(handler)

    def _agent_ws_url(self, instance_id: str, agent_port: int) -> str:
        """YuanRong frontend 的 instance WS 代理地址.

        形如 ``ws://<frontend-host>:8888/serverless/v1/ws?instance=<id>&tenant_id=default&port=<port>``，
        其中 ``instance`` 是 create 返回的 instanceID，``port`` 是 create cmds 里
        agentserver 监听的端口。
        """
        frontend = str(self._yuanrong.frontend_endpoint or "").rstrip("/")
        parsed = urllib.parse.urlsplit(frontend)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        query = urllib.parse.urlencode(
            {
                "instance": instance_id,
                "tenant_id": self._yuanrong.agent_namespace or "default",
                "port": str(agent_port),
            }
        )
        return f"{ws_scheme}://{parsed.netloc}/serverless/v1/ws?{query}"

    async def _connect_ws_until_ready(
        self,
        *,
        instance_id: str,
        agent_port: int,
        user_id: str = "",
        session_id: str = "",
        agent_type: str = "",
    ) -> WebSocketAgentServerClient:
        """建立到 instance 的 WS；对冷启动 502 等做 deadline 内重试."""
        uri = self._agent_ws_url(instance_id, agent_port)
        deadline = asyncio.get_running_loop().time() + _WS_CONNECT_READY_TIMEOUT_SECONDS
        attempt = 0

        while True:
            attempt += 1
            client = self._ws_client_factory()
            if self._push_handler is not None:
                client.set_server_push_handler(self._push_handler)
            log_agentos(
                logger,
                logging.DEBUG,
                "agent.ws.connecting",
                user_id=user_id,
                session_id=session_id,
                sandbox_id=instance_id,
                agent_type=agent_type,
                instance=instance_id,
                attempt=attempt,
            )
            try:
                await client.connect(uri)
                log_agentos(
                    logger,
                    logging.INFO,
                    "agent.ws.ready",
                    user_id=user_id,
                    session_id=session_id,
                    sandbox_id=instance_id,
                    agent_type=agent_type,
                    instance=instance_id,
                    attempt=attempt,
                )
                return client
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    logger.warning(
                        "[AgentOS] agent.ws.cleanup.fail user_id=%s sandbox_id=%s attempt=%s",
                        user_id,
                        instance_id,
                        attempt,
                        extra=agentos_extra(
                            session_id=session_id,
                            sandbox_id=instance_id,
                        ),
                        exc_info=True,
                    )
                remaining = deadline - asyncio.get_running_loop().time()
                give_up = remaining <= 0 or not _is_ws_connect_retryable(exc)
                if give_up:
                    # Last failure summary: emit one WARNING then give up.
                    log_agentos(
                        logger,
                        logging.WARNING,
                        "agent.ws.retry",
                        user_id=user_id,
                        session_id=session_id,
                        sandbox_id=instance_id,
                        agent_type=agent_type,
                        instance=instance_id,
                        attempt=attempt,
                        error=type(exc).__name__,
                        final="yes",
                    )
                    raise

                sleep_for = min(_WS_CONNECT_RETRY_INTERVAL_SECONDS, remaining)
                retry_level = (
                    logging.WARNING if _should_log_ws_retry(attempt) else logging.DEBUG
                )
                log_agentos(
                    logger,
                    retry_level,
                    "agent.ws.retry",
                    user_id=user_id,
                    session_id=session_id,
                    sandbox_id=instance_id,
                    agent_type=agent_type,
                    instance=instance_id,
                    attempt=attempt,
                    error=type(exc).__name__,
                    sleep=f"{sleep_for:.1f}s",
                )
                await asyncio.sleep(sleep_for)

    async def _get_ws_client(self, runtime: AgentRuntime) -> WebSocketAgentServerClient:
        """获取（或建立）到该 agent instance 的 WS 直连，不走 invoke 链路.

        冷启动时 create 返回早于 agentserver listen：对可重试错误做就绪等待。
        同一 instance 的并发首连合并到一个 Future，避免多路同时打 502。
        """
        info = runtime.info
        instance_id = str(info.sandbox_id or "").strip()
        if not instance_id:
            raise ValueError(
                f"agent has no sandbox instance for ws connect: "
                f"user={info.user_id} agent_type={info.agent_type}"
            )
        raw_port = info.metadata.get("agent_port")
        try:
            agent_port = int(raw_port)
        except (TypeError, ValueError):
            raise ValueError(
                f"agent has no agent_port metadata for ws connect: "
                f"instance={instance_id} agent_type={info.agent_type}"
            ) from None

        async with self._ws_clients_lock:
            existing = self._ws_clients.get(instance_id)
            if existing is not None:
                return existing
            inflight = self._ws_connecting.get(instance_id)
            if inflight is None:
                inflight = asyncio.get_running_loop().create_future()
                self._ws_connecting[instance_id] = inflight
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            return await asyncio.shield(inflight)

        try:
            client = await self._connect_ws_until_ready(
                instance_id=instance_id,
                agent_port=agent_port,
                user_id=str(info.user_id or ""),
                session_id=str(info.metadata.get("session_id") or ""),
                agent_type=str(info.agent_type or ""),
            )
        except Exception as exc:
            async with self._ws_clients_lock:
                self._ws_connecting.pop(instance_id, None)
                if not inflight.done():
                    inflight.set_exception(exc)
            raise

        async with self._ws_clients_lock:
            self._ws_clients[instance_id] = client
            self._ws_connecting.pop(instance_id, None)
            if not inflight.done():
                inflight.set_result(client)
        return client

    async def _close_ws_client(self, instance_id: str | None) -> None:
        if not instance_id:
            return
        key = str(instance_id)
        async with self._ws_clients_lock:
            client = self._ws_clients.pop(key, None)
            inflight = self._ws_connecting.pop(key, None)
        if inflight is not None and not inflight.done():
            inflight.cancel()
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            logger.warning(
                "[AgentOS] agent.ws.close.fail sandbox_id=%s",
                instance_id,
                extra={"sandbox_id": str(instance_id)},
                exc_info=True,
            )

    async def _close_all_ws_clients(self) -> None:
        async with self._ws_clients_lock:
            clients = list(self._ws_clients.values())
            self._ws_clients.clear()
            inflight = list(self._ws_connecting.values())
            self._ws_connecting.clear()
        for fut in inflight:
            if not fut.done():
                fut.cancel()
        for client in clients:
            try:
                await client.disconnect()
            except Exception:
                logger.warning("[AgentOSRouter] close agent ws failed", exc_info=True)

    async def send_request(
        self,
        envelope: E2AEnvelope,
        *,
        timeout: float | None = None,
    ) -> AgentResponse:
        # 3rdagent.list / 3rdagent.switch are handled by Gateway ThirdAgent
        # (TUI local_handler), not via E2A send_request.
        if self._is_ssh_relay_request(envelope):
            return await self._handle_ssh_relay(envelope)
        await self._inject_external_cli_agents(envelope)
        try:
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout, AgentCreateFailed) as exc:
            self._log_route("route.error", envelope, level=logging.WARNING, error=str(exc))
            return self._routing_error_response(envelope, str(exc))
        try:
            runtime.attach_to_envelope(envelope)
            if not self._uses_direct_yuanrong(runtime.info.agent_type):
                return self._routing_error_response(
                    envelope,
                    f"agent_type={runtime.info.agent_type} does not use websocket; "
                    "use 3rdagent.switch / SSH",
                )
            # create 后通过 YuanRong frontend WS 代理直连 instance，不走 invoke。
            try:
                ws_client = await self._get_ws_client(runtime)
            except ValueError as exc:
                self._log_route(
                    "route.error",
                    envelope,
                    runtime,
                    level=logging.WARNING,
                    error=str(exc),
                )
                return self._routing_error_response(envelope, str(exc))
            except Exception as exc:
                if not _is_agent_network_error(exc):
                    raise
                error = f"agent server unreachable: {exc}"
                self._log_route(
                    "route.error", envelope, runtime, level=logging.WARNING, error=error
                )
                await self._cleanup_agent_on_network_failure(
                    runtime, reason=type(exc).__name__
                )
                return self._routing_error_response(envelope, error)
            self._log_route("route.unary", envelope, runtime)
            try:
                return await ws_client.send_request(envelope, timeout=timeout)
            except Exception as exc:
                if not _is_agent_network_error(exc):
                    raise
                error = f"agent server request failed: {exc}"
                self._log_route(
                    "route.error", envelope, runtime, level=logging.WARNING, error=error
                )
                await self._cleanup_agent_on_network_failure(
                    runtime, reason=type(exc).__name__
                )
                return self._routing_error_response(envelope, error)
        finally:
            await self._agent_manager.release(runtime.key)

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        await self._inject_external_cli_agents(envelope)
        try:
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout, AgentCreateFailed) as exc:
            self._log_route("route.error", envelope, level=logging.WARNING, error=str(exc))
            yield self._routing_error_chunk(envelope, str(exc))
            return
        try:
            runtime.attach_to_envelope(envelope)
            if not self._uses_direct_yuanrong(runtime.info.agent_type):
                yield self._routing_error_chunk(
                    envelope,
                    f"agent_type={runtime.info.agent_type} does not use websocket; "
                    "use 3rdagent.switch / SSH",
                )
                return
            # create 后通过 YuanRong frontend WS 代理直连 instance，不走 invoke。
            try:
                ws_client = await self._get_ws_client(runtime)
            except ValueError as exc:
                self._log_route(
                    "route.error",
                    envelope,
                    runtime,
                    level=logging.WARNING,
                    error=str(exc),
                )
                yield self._routing_error_chunk(envelope, str(exc))
                return
            except Exception as exc:
                if not _is_agent_network_error(exc):
                    raise
                error = f"agent server unreachable: {exc}"
                self._log_route(
                    "route.error", envelope, runtime, level=logging.WARNING, error=error
                )
                await self._cleanup_agent_on_network_failure(
                    runtime, reason=type(exc).__name__
                )
                yield self._routing_error_chunk(envelope, error)
                return
            self._log_route("route.stream", envelope, runtime)
            try:
                async for chunk in ws_client.send_request_stream(envelope):
                    yield chunk
            except Exception as exc:
                if not _is_agent_network_error(exc):
                    raise
                error = f"agent server request failed: {exc}"
                self._log_route(
                    "route.error", envelope, runtime, level=logging.WARNING, error=error
                )
                await self._cleanup_agent_on_network_failure(
                    runtime, reason=type(exc).__name__
                )
                yield self._routing_error_chunk(envelope, error)
                return
        finally:
            await self._agent_manager.release(runtime.key)

    # ---------- external_cli_agents injection for team chat send ----------

    async def _inject_external_cli_agents(self, envelope: E2AEnvelope) -> None:
        """Inject ``external_cli_agents`` into params for team chat send.

        When the request is a team-mode chat send, fetches registered
        3rd-party agents from the registry and constructs
        ``external_cli_agents`` with SSH transport info for each, so the
        builtin agent (inside the container) can SSH into each 3rd-party
        agent through the gateway's northbound SSH channel.
        """
        if not _is_team_mode(envelope.params):
            return
        user_id = str(envelope.user_id or "").strip()
        if not user_id:
            return
        ssh_fields = self._ssh_endpoint_fields()
        if ssh_fields is None:
            logger.warning(
                "[AgentOSRouter] skip external_cli_agents: ssh endpoint unavailable"
            )
            return
        try:
            images = await self._registry.list_user_images(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentOSRouter] list_user_images failed: %s", exc)
            return
        key_file = self._resolve_ssh_key_file(user_id)
        agents: list[dict[str, Any]] = []
        for image in images:
            agent_type = str(
                (image.metadata or {}).get("agent_type") or image.image_name or ""
            ).strip()
            if not agent_type or not is_third_party_agent_type(agent_type):
                continue
            agents.append(
                {
                    "cli_agent": agent_type,
                    "ssh_transport": {
                        "host": ssh_fields["ssh_ip"],
                        "port": ssh_fields["ssh_port"],
                        "username": user_id,
                        "agent": False,
                        "key_file": key_file,
                        "disable_host_key_check": False,
                        "use_exec": False
                    },
                }
            )
        if agents:
            params = envelope.params if isinstance(envelope.params, dict) else {}
            params["external_cli_agents"] = agents
            logger.info(
                "[AgentOSRouter] injected external_cli_agents: user=%s count=%d",
                user_id,
                len(agents),
            )

    def _resolve_ssh_key_file(self, user_id: str) -> str:
        """Resolve the SSH key file path for external_cli_agents."""
        keys_dir_template = DEFAULT_CLIENT_KEYS_DIR
        if self._ssh_relay is not None:
            keys_dir_template = self._ssh_relay.client_keys_dir
        keys_dir = resolve_client_keys_dir(keys_dir_template, user_id)
        return str(keys_dir / "id_ed25519")

    async def thirdagent_list(
        self,
        *,
        user_id: str,
        current_agent_type: str = "",
    ) -> dict[str, Any]:
        """Handle ``3rdagent.list``: list switchable third-party agent images."""
        uid = str(user_id or "").strip()
        if not uid:
            return {
                "ok": False,
                "error": "user_id is required for AgentOS routing",
                "code": "BAD_REQUEST",
            }
        images = await self._registry.list_user_images(uid)
        agents: list[dict[str, Any]] = []
        for image in images:
            agent_type = str(
                (image.metadata or {}).get("agent_type") or image.image_name or ""
            ).strip()
            if not agent_type:
                continue
            agents.append(
                {
                    "agent_type": agent_type,
                    "image_name": image.image_name,
                    "image_uri": image.image_uri,
                    "metadata": dict(image.metadata or {}),
                }
            )
        current = (
            str(current_agent_type or "").strip()
            or self.get_current_agent_type(uid)
        )
        return {
            "ok": True,
            "payload": {
                "agents": agents,
                "current_agent_type": current,
            },
        }

    def _ssh_endpoint_fields(self) -> dict[str, Any] | None:
        """Northbound ``channels.ssh`` listen ip/port, or None if unavailable."""
        endpoint = self._ssh_channel_endpoint
        if endpoint is None:
            return None
        ip = str(endpoint.ip or "").strip()
        port = int(endpoint.port or 0)
        if not ip or port <= 0:
            return None
        return {"ssh_ip": ip, "ssh_port": port}

    @staticmethod
    def _missing_ssh_endpoint_error() -> dict[str, Any]:
        return {
            "ok": False,
            "error": (
                "ssh channel endpoint is unavailable: enable channels.ssh "
                "and set listen_host / listen_port"
            ),
            "code": "SSH_ENDPOINT_UNAVAILABLE",
        }

    async def thirdagent_switch(
        self,
        *,
        user_id: str,
        agent_type: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Handle ``3rdagent.switch``: ensure agent exists without forwarding chat.

        Success payload includes northbound SSH channel ``ssh_ip``/``ssh_port``
        (``channels.ssh.listen_host`` / ``listen_port``). Missing values fail.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return {
                "ok": False,
                "error": "user_id is required for AgentOS routing",
                "code": "BAD_REQUEST",
            }
        try:
            normalized = AgentRuntime.normalize_agent_type(agent_type)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "UNSUPPORTED_AGENT_TYPE",
            }
        # Fail fast before create when northbound SSH channel is not configured.
        ssh_fields = self._ssh_endpoint_fields()
        if ssh_fields is None:
            return self._missing_ssh_endpoint_error()
        # Fail fast before create: without the key the client cannot pass
        # SSH public-key auth, so a "successful" switch would be unusable.
        try:
            key_fields = self._ephemeral_ssh_key_fields(
                user_id=uid,
                session_id=session_id,
            )
        except EphemeralKeyIssueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "SSH_KEY_ISSUE_FAILED",
            }
        # Builtin swarm: no registry / create_sandbox; mark current type only.
        if self._uses_direct_yuanrong(normalized):
            self._current_agent_types[uid] = normalized
            payload = {
                "agent_id": "",
                "agent_type": normalized,
                "sandbox_id": "",
                "status": AgentStatus.READY.value,
                **ssh_fields,
                **key_fields,
            }
            return {"ok": True, "payload": payload}
        try:
            runtime = await self._agent_manager.get_or_create_agent(
                uid,
                normalized,
                key_values={"session_id": session_id} if session_id else None,
                creator=self._create_agent,
                metadata={"session_id": session_id} if session_id else None,
            )
        except (ValueError, AgentCreatingTimeout, AgentCreateFailed) as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "INTERNAL_ERROR",
            }
        info = runtime.info
        status = info.status.value if hasattr(info.status, "value") else str(info.status)
        instance_id = str(info.sandbox_id or "").strip()
        ssh_relay = self._ssh_relay
        if ssh_relay is not None:
            if not instance_id:
                return {
                    "ok": False,
                    "error": f"agent has no yuanrong instance_id: user={uid}",
                    "code": "INTERNAL_ERROR",
                }
            try:
                # create 返回不代表 sshd 已听端口；等南向 SSH 通了再让客户端连。
                await ssh_relay.wait_until_ready(instance_id, user_id=uid)
            except Exception as exc:
                log_agentos(
                    logger,
                    logging.WARNING,
                    "ssh.south.not_ready",
                    user_id=uid,
                    session_id=session_id,
                    sandbox_id=instance_id,
                    instance=instance_id,
                    error=type(exc).__name__,
                )
                return {
                    "ok": False,
                    "error": f"sandbox sshd not ready: {exc}",
                    "code": "SSH_NOT_READY",
                }
        # 记录用户当前 agent_type，后续 SSH 接入默认跟随
        self._current_agent_types[uid] = normalized
        payload = {
            "agent_id": info.agent_id,
            "agent_type": info.agent_type,
            "sandbox_id": info.sandbox_id,
            "status": status,
            **ssh_fields,
            **key_fields,
        }
        return {"ok": True, "payload": payload}

    def _ephemeral_ssh_key_fields(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Mint ``ssh_private_key`` when an issuer is configured.

        Returns an empty mapping when no issuer is injected (auth disabled).
        Raises :class:`EphemeralKeyIssueError` when issuance is expected but
        does not yield a usable key.
        """
        issuer = self._key_issuer
        if issuer is None:
            return {}
        try:
            private_key = issuer.issue_ephemeral_key(
                user_id=user_id,
                username=user_id,
                session_id=str(session_id or ""),
                ttl_sec=self._ephemeral_key_ttl_sec,
            )
        except Exception as exc:
            logger.error(
                "[AgentOSRouter] failed to issue ephemeral SSH key: user=%s error=%s",
                user_id,
                exc,
            )
            raise EphemeralKeyIssueError(
                f"failed to issue ephemeral SSH key: {exc}"
            ) from exc
        if not private_key:
            logger.error(
                "[AgentOSRouter] ephemeral SSH key issuer returned an empty key: user=%s",
                user_id,
            )
            raise EphemeralKeyIssueError("ephemeral SSH key issuer returned an empty key")
        return {"ssh_private_key": private_key}

    async def shutdown(self) -> None:
        try:
            await self.disconnect()
        finally:
            auth_client = self._auth_client
            close = getattr(auth_client, "aclose", None)
            if callable(close):
                await close()

    # ---------- SSH relay (northbound SshChannel -> YuanRong instance) ----------

    @staticmethod
    def _is_ssh_relay_request(envelope: E2AEnvelope) -> bool:
        return str(envelope.method or "") == ReqMethod.SSH_RELAY.value

    async def _handle_ssh_relay(self, envelope: E2AEnvelope) -> AgentResponse:
        """Start the southbound SSH relay for an ``ssh.relay`` request.

        Agent resolution (YuanRong instance creation) and the PTY relay run
        in a background task so the gateway forward loop is not blocked for
        the whole SSH session; the northbound channel waits on the relay
        session ``done`` event instead of this response.
        """
        session_id = str(envelope.session_id or "")
        params = envelope.params if isinstance(envelope.params, dict) else {}
        # Live SshRelaySession handed over in-process by the northbound
        # SshChannel; pop it so it never leaks into serialization/logging.
        relay_session = params.pop("relay_session", None)
        if relay_session is None:
            return self._routing_error_response(
                envelope, f"ssh relay session not found in params: {session_id}"
            )
        if self._ssh_relay is None:
            msg = "ssh relay is not configured for AgentOS router"
            relay_session.exit_code = 1
            relay_session.done.set()
            return self._routing_error_response(envelope, msg)

        task = asyncio.create_task(
            self._run_ssh_relay(envelope, relay_session),
            name=f"agentos-ssh-relay-{session_id[:24]}",
        )
        relay_session.relay_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            ok=True,
            payload={"method": ReqMethod.SSH_RELAY.value, "status": "relay_started"},
        )

    async def _run_ssh_relay(self, envelope: E2AEnvelope, relay_session: Any) -> None:
        ssh_relay = self._ssh_relay
        if ssh_relay is None:
            # _handle_ssh_relay already guards this; keep a safe fallback.
            relay_session.exit_code = 1
            relay_session.done.set()
            return
        self._apply_current_agent_type_for_ssh(envelope)
        try:
            agent_type = self._extract_agent_type(envelope)
            if self._uses_direct_yuanrong(agent_type):
                ssh_relay.fail_session(
                    relay_session,
                    "builtin agent_type has no AgentOS sandbox for SSH; run "
                    "3rdagent.switch first or provide a remote command so "
                    "agent_type can be derived from its first token",
                )
                return
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout, AgentCreateFailed, AgentDeleted) as exc:
            log_agentos(
                logger,
                logging.WARNING,
                "ssh.relay.fail",
                user_id=str(envelope.user_id or ""),
                session_id=str(relay_session.session_id or ""),
                request_id=str(envelope.request_id or ""),
                channel="ssh",
                error=str(exc),
            )
            ssh_relay.fail_session(
                relay_session, f"agent resolve failed: {exc}"
            )
            return
        except Exception as exc:  # noqa: BLE001 - creation errors must release the client
            logger.exception(
                format_agentos(
                    "ssh.relay.fail",
                    user_id=str(envelope.user_id or ""),
                    session_id=str(relay_session.session_id or ""),
                    request_id=str(envelope.request_id or ""),
                    agent_type=str(envelope.params.get("agent_type") or "")
                    if isinstance(envelope.params, dict)
                    else "",
                    error=type(exc).__name__,
                    channel="ssh",
                ),
            )
            ssh_relay.fail_session(
                relay_session, f"agent creation failed: {exc}"
            )
            return

        # Hold the task count for the whole SSH session so the idle reaper
        # never reclaims a sandbox with a live (even silent) SSH connection.
        try:
            instance_id = str(runtime.info.sandbox_id or "").strip()
            if not instance_id:
                ssh_relay.fail_session(
                    relay_session,
                    f"agent has no yuanrong instance_id: user={runtime.info.user_id}",
                )
                return

            runtime.attach_to_envelope(envelope)
            log_agentos(
                logger,
                logging.INFO,
                "ssh.relay.start",
                user_id=runtime.info.user_id,
                session_id=str(relay_session.session_id or ""),
                request_id=str(envelope.request_id or ""),
                sandbox_id=instance_id,
                agent_type=runtime.info.agent_type,
                instance=instance_id,
                channel="ssh",
            )
            await ssh_relay.run(
                relay_session,
                instance_id,
                user_id=runtime.info.user_id,
            )
        finally:
            await self._agent_manager.release(runtime.key)

    def _apply_current_agent_type_for_ssh(self, envelope: E2AEnvelope) -> None:
        """SSH 接入跟随用户当前 agent_type（由 3rdagent.switch 记录）。

        未 switch / 仍为内置 ``jiuwenswarm`` 时，取 SSH 远程指令首词作为
        agent_type。
        """
        params = envelope.params if isinstance(envelope.params, dict) else {}
        if str(params.get("agent_type") or "").strip():
            return
        user_id = str(envelope.user_id or "").strip()
        current = self.get_current_agent_type(user_id)
        if self._uses_direct_yuanrong(current):
            command = str(params.get("command") or "").strip()
            if not command:
                ctx = envelope.channel_context
                if isinstance(ctx, dict):
                    command = str(ctx.get("command") or "").strip()
            token = command.split(maxsplit=1)[0].lower() if command else ""
            current = token or BUILTIN_AGENT_TYPE
        params = dict(params)
        params["agent_type"] = current
        envelope.params = params
        logger.info(
            "[AgentOS] ssh.relay.agent_type user_id=%s agent_type=%s",
            user_id,
            current,
        )

    async def _drain_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _resolve_agent(
        self,
        envelope: E2AEnvelope,
        *,
        acquire: bool = False,
    ) -> AgentRuntime:
        user_id = self._extract_user_id(envelope)
        agent_type = self._extract_agent_type(envelope)
        return await self._agent_manager.get_or_create_agent(
            user_id,
            agent_type,
            key_values={"session_id": envelope.session_id},
            creator=self._create_agent,
            metadata={"session_id": envelope.session_id},
            acquire=acquire,
        )

    # ---------- idle sandbox reclamation ----------

    def _idle_reaper_enabled(self) -> bool:
        return self._sandbox_idle_timeout_seconds > 0

    def _ensure_idle_reaper_task(self) -> None:
        if self._closed or not self._idle_reaper_enabled():
            return
        if self._idle_reaper_task is not None and not self._idle_reaper_task.done():
            return
        self._idle_reaper_task = asyncio.create_task(
            self._idle_reaper_loop(),
            name="agentos-sandbox-idle-reaper",
        )

    async def _stop_idle_reaper_task(self) -> None:
        task = self._idle_reaper_task
        self._idle_reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _idle_reaper_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._sandbox_idle_check_interval_seconds)
            try:
                await self._reap_idle_once()
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                logger.exception("[AgentOSRouter] idle sandbox reap pass failed")

    async def _reap_idle_once(self) -> int:
        """Reclaim agents idle beyond the timeout; returns the reclaimed count.

        Delegates to :meth:`delete_agent` with ``idle_timeout_seconds`` so the
        same sandbox + registry cleanup path is used. ``pop_if_idle`` inside
        ``delete_agent`` re-checks READY / ``task_count == 0`` / staleness under
        the manager lock, so a concurrent acquire can never lose its sandbox.
        """
        if not self._idle_reaper_enabled():
            return 0
        reaped = 0
        for key in await self._agent_manager.list_keys():
            values = dict(zip(self._agent_manager.key_fields, key, strict=False))
            user_id = str(values.pop("user_id", "") or "").strip()
            agent_type = str(values.pop("agent_type", "") or "").strip()
            if not user_id or not agent_type:
                continue
            try:
                deleted = await self.delete_agent(
                    user_id,
                    agent_type,
                    key_values=values or None,
                    idle_timeout_seconds=self._sandbox_idle_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - keep reaping other agents
                logger.exception(
                    "[AgentOS] sandbox.reclaim.fail user_id=%s agent_type=%s",
                    user_id,
                    agent_type,
                )
                continue
            if deleted:
                reaped += 1
        return reaped

    async def _create_agent(self, agent_info: AgentInfo) -> AgentInfo:
        workspace = resolve_agent_workspace(
            agent_info.user_id,
            workspace_root=self._workspace_root,
        )
        # runtime_spec 获取方式因 agent_type 而异
        env_vars: dict[str, str] | None = None
        if agent_info.agent_type == BUILTIN_AGENT_TYPE:
            # jiuwenswarm: 不从注册中心获取镜像信息，使用内置 runtime_spec
            port = 18092
            runtime_spec: dict[str, Any] = {
                "sandbox_type": "supervisor",
                "runtime": "python3.11",
                "rootfs": {
                    "imageurl": f"{BUILTIN_AGENT_TYPE}-agent-runtime:latest",
                    "user": "agentos",
                },
                "cmds": [["sh", "-c", f"exec jiuwenswarm-agentserver --port {port}"]],
                "cpu": int(os.environ.get("AGENTOS_BUILTIN_AGENT_CPU", "2000")),
                "memory": int(os.environ.get("AGENTOS_BUILTIN_AGENT_MEMORY", "4096"))
            }
            # 不注入 AGENT_SERVER_HOST: 留空让沙箱内 agentserver 自行检测沙箱本地
            # 非 loopback IP(ISOLATED 模式 bind veth 地址,外部可达;见
            # app_agentserver._resolve_bind_host)。单机版默认仍 127.0.0.1。
            env_vars = {
                USER_DIRECTORY_ENV_KEY: workspace,
            }
            # create 后 Gateway 通过 frontend WS 代理直连该端口（不走 invoke）。
            extra_metadata: dict[str, Any] = {"agent_port": port}
        else:
            image_info = await self._registry.get_image_info(agent_info.agent_type)
            runtime_spec = build_inline_runtime_spec(image_info)
            env_raw = image_info.metadata.get("env_vars")
            env_vars = (
                {str(k): str(v) for k, v in dict(env_raw).items()}
                if isinstance(env_raw, dict) and env_raw
                else None
            )
            extra_metadata = {"image_info": dict(image_info.metadata)}
            # 3rdagent 走 SSH，不连 agentserver WS；create 的 rootfs 不传 ports。
            rootfs = runtime_spec.get("rootfs")
            if isinstance(rootfs, dict) and "ports" in rootfs:
                runtime_spec["rootfs"] = {
                    key: value for key, value in rootfs.items() if key != "ports"
                }

        started = time.monotonic()
        sandbox = await self._yuanrong.create_sandbox(
            namespace=self._yuanrong.agent_namespace,
            name=f"{agent_info.user_id}+{agent_info.agent_type}",
            workspace=workspace,
            runtime_spec=runtime_spec,
            env_vars=env_vars,
        )
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        instance_id = sandbox.sandbox_id
        agent_info.sandbox_id = instance_id
        session_id = str(agent_info.metadata.get("session_id") or "")
        agent_info.metadata.update(
            {
                "instance_id": instance_id,
                "workspace": workspace,
                "runtime_spec": dict(runtime_spec),
                **extra_metadata,
                "sandbox": dict(sandbox.metadata),
            }
        )
        agent_info.status = AgentStatus.READY
        log_agentos(
            logger,
            logging.INFO,
            "sandbox.create.ok",
            user_id=agent_info.user_id,
            session_id=session_id,
            sandbox_id=instance_id,
            agent_type=agent_info.agent_type,
            instance=instance_id,
            latency_ms=latency_ms,
        )

        task = asyncio.create_task(
            self._register_agent(agent_info.copy()),
            name=f"agentos-register-{agent_info.agent_id[:12]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return agent_info

    async def delete_agent(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: dict[str, Any] | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> bool:
        """Delete agent mapping, release its YuanRong sandbox, unregister registry.

        When ``idle_timeout_seconds`` is set, only delete a READY agent that is
        unheld (``task_count == 0``) and idle beyond the timeout. Returns whether
        an agent was deleted.
        """
        resolved_key_values = dict(key_values or {})
        if idle_timeout_seconds is not None:
            return await self._delete_idle_agent(
                user_id,
                agent_type,
                key_values=resolved_key_values,
                idle_timeout_seconds=float(idle_timeout_seconds),
            )

        runtime = await self._agent_manager.get_agent(
            user_id, agent_type, key_values=resolved_key_values or None
        )
        if runtime is None:
            return False
        agent_info = runtime.info
        if (
            "session_id" not in resolved_key_values
            and agent_info.metadata.get("session_id")
        ):
            resolved_key_values["session_id"] = agent_info.metadata.get(
                "session_id"
            )
        await self._close_ws_client(agent_info.sandbox_id)
        if agent_info.sandbox_id:
            await self._yuanrong.delete_sandbox(agent_info.sandbox_id)
        await self._agent_manager.delete_agent(
            agent_info.user_id,
            agent_info.agent_type,
            key_values=resolved_key_values or None,
        )
        await self._unregister_agent(agent_info)
        return True

    async def _delete_idle_agent(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: dict[str, Any],
        idle_timeout_seconds: float,
    ) -> bool:
        """Atomically pop an idle agent then run shared delete cleanup."""
        key = AgentRuntime.build_key(
            self._agent_manager.key_fields,
            user_id=user_id,
            agent_type=agent_type,
            key_values=key_values or None,
        )
        runtime = await self._agent_manager.pop_if_idle(key, idle_timeout_seconds)
        if runtime is None:
            return False
        agent_info = runtime.info
        log_agentos(
            logger,
            logging.INFO,
            "sandbox.reclaim",
            user_id=agent_info.user_id,
            session_id=str(agent_info.metadata.get("session_id") or ""),
            sandbox_id=str(agent_info.sandbox_id or ""),
            agent_type=agent_info.agent_type,
            instance=str(agent_info.sandbox_id or ""),
            idle_timeout=f"{idle_timeout_seconds:.0f}s",
        )
        await self._release_agent_resources(agent_info, best_effort=True)
        return True

    async def _cleanup_agent_on_network_failure(
        self,
        runtime: AgentRuntime,
        *,
        reason: str,
    ) -> None:
        """AgentServer 网络层错误后的强制清理。

        instance 已不可达（连接重试耗尽 / 请求超时 / 连接断开）时复用
        :meth:`delete_agent` 的强制删除路径（不检查 task_count / idle）：
        关闭 WS 直连 → 删除 YuanRong 沙箱 → 移除内存 runtime → 注销注册中心
        instance 条目。后续请求会触发重新建沙箱，避免死沙箱与僵尸注册条目。
        """
        info = runtime.info
        session_id = str(info.metadata.get("session_id") or "")
        sandbox_id = str(info.sandbox_id or "")
        logger.warning(
            "[AgentOSRouter] sandbox cleanup on network failure: "
            "user_id=%s session_id=%s sandbox_id=%s agent_type=%s reason=%s",
            info.user_id,
            session_id,
            sandbox_id,
            info.agent_type,
            reason,
        )
        key_values: dict[str, Any] | None = None
        if "session_id" in self._agent_manager.key_fields and session_id:
            key_values = {"session_id": session_id}
        try:
            await self.delete_agent(
                info.user_id,
                info.agent_type,
                key_values=key_values,
            )
        except Exception:  # noqa: BLE001 - cleanup must not mask the route error
            logger.exception(
                "[AgentOSRouter] network-failure cleanup failed: "
                "user_id=%s agent_type=%s sandbox_id=%s",
                info.user_id,
                info.agent_type,
                sandbox_id,
            )

    async def _release_agent_resources(
        self,
        agent_info: AgentInfo,
        *,
        best_effort: bool = False,
    ) -> None:
        """Delete YuanRong sandbox and unregister the registry instance."""
        await self._close_ws_client(agent_info.sandbox_id)
        if agent_info.sandbox_id:
            try:
                await self._yuanrong.delete_sandbox(agent_info.sandbox_id)
            except Exception:
                logger.exception(
                    format_agentos(
                        "sandbox.delete.fail",
                        user_id=agent_info.user_id,
                        session_id=str(agent_info.metadata.get("session_id") or ""),
                        sandbox_id=str(agent_info.sandbox_id or ""),
                        agent_type=agent_info.agent_type,
                        instance=str(agent_info.sandbox_id or ""),
                    ),
                    extra=agentos_extra(
                        session_id=str(agent_info.metadata.get("session_id") or ""),
                        sandbox_id=str(agent_info.sandbox_id or ""),
                    ),
                )
                if not best_effort:
                    raise
        try:
            await self._unregister_agent(agent_info)
        except Exception:
            logger.exception(
                "[AgentOSRouter] unregister agent failed: agent_id=%s",
                agent_info.agent_id,
            )
            if not best_effort:
                raise

    async def _unregister_agent(self, agent_info: AgentInfo) -> None:
        await self._registry.unregister_agent(
            agent_info.agent_id,
            user_id=agent_info.user_id,
            agent_type=agent_info.agent_type,
        )

    async def _register_agent(self, agent_info: AgentInfo) -> None:
        # 网络失败清理（delete_agent 强制路径）可能先于本后台任务执行：注册前
        # 确认内存 runtime 仍存在（强制删除会 pop 掉 runtime），避免清理注销后
        # 又把僵尸 instance 条目写回注册中心。
        session_id = str(agent_info.metadata.get("session_id") or "")
        key_values: dict[str, Any] | None = None
        if "session_id" in self._agent_manager.key_fields and session_id:
            key_values = {"session_id": session_id}
        live = await self._agent_manager.get_agent(
            agent_info.user_id,
            agent_info.agent_type,
            key_values=key_values,
        )
        if live is None:
            logger.info(
                "[AgentOSRouter] registry register skipped (agent already "
                "cleaned up): user_id=%s session_id=%s sandbox_id=%s agent_type=%s",
                agent_info.user_id,
                session_id,
                str(agent_info.sandbox_id or ""),
                agent_info.agent_type,
            )
            return
        try:
            await self._registry.register_agent(agent_info)
        except Exception:
            logger.exception(
                "[AgentOSRouter] async registry registration failed: agent_id=%s",
                agent_info.agent_id,
            )
            return

        # 创建后查询 YuanRong 获取 node_ip / sandbox_ip，更新注册中心 instance
        # 的 placement 字段（node + address），供调度/路由使用。
        try:
            instance_info = await self._yuanrong.get_agent_info(
                agent_info.sandbox_id
            )
            node_ip = str(instance_info.get("node_ip") or "").strip()
            sandbox_ip = str(instance_info.get("sandbox_ip") or "").strip()
            if node_ip or sandbox_ip:
                service_id = instance_service_id(
                    agent_info.user_id, agent_info.agent_type
                )
                await self._registry.update_instance(
                    service_id,
                    node=node_ip or None,
                    address=sandbox_ip or None,
                )
                log_agentos(
                    logger,
                    logging.INFO,
                    "registry.instance.ok",
                    user_id=agent_info.user_id,
                    session_id=str(agent_info.metadata.get("session_id") or ""),
                    sandbox_id=str(agent_info.sandbox_id or ""),
                    agent_type=agent_info.agent_type,
                    instance=str(agent_info.sandbox_id or ""),
                    service_id=service_id,
                    node=node_ip,
                    address=sandbox_ip,
                )
        except Exception:
            logger.exception(
                format_agentos(
                    "registry.instance.fail",
                    user_id=agent_info.user_id,
                    session_id=str(agent_info.metadata.get("session_id") or ""),
                    sandbox_id=str(agent_info.sandbox_id or ""),
                    agent_type=agent_info.agent_type,
                    instance=str(agent_info.sandbox_id or ""),
                    error="update_failed",
                ),
                extra=agentos_extra(
                    session_id=str(agent_info.metadata.get("session_id") or ""),
                    sandbox_id=str(agent_info.sandbox_id or ""),
                ),
            )

    @staticmethod
    def _extract_user_id(envelope: E2AEnvelope) -> str:
        user_id = str(envelope.user_id or "").strip()
        if not user_id:
            raise ValueError("user_id is required for AgentOS routing")
        return user_id

    @staticmethod
    def _extract_agent_type(envelope: E2AEnvelope) -> str:
        raw = envelope.params.get("agent_type")
        if raw is None:
            raw = envelope.channel_context.get("agent_type")
        try:
            return AgentRuntime.normalize_agent_type(raw)
        except ValueError as exc:
            raise UnsupportedAgentType(str(exc)) from exc

    @staticmethod
    def _routing_error_response(
        envelope: E2AEnvelope,
        message: str,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            ok=False,
            payload={"error": message},
        )

    @staticmethod
    def _routing_error_chunk(
        envelope: E2AEnvelope,
        message: str,
    ) -> AgentResponseChunk:
        return AgentResponseChunk(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            payload={"error": message},
            is_complete=True,
        )
