# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Transport-agnostic Web RPC host: method registry, history capture, handler invoke."""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import inspect
import ipaddress
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import aiohttp

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.gateway.channel_manager.web.file_http import (
    is_path_under_directory,
    safe_filename,
)
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)

logger = logging.getLogger(__name__)

MethodHandler = Callable[..., Awaitable[None]]
ConnectHook = Callable[..., Any]

HANDLER_BEFORE_CALLBACK_METHODS = frozenset({ReqMethod.CHAT_SEND.value})
_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024


def _is_url_safe_for_fetch(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


@dataclass(frozen=True)
class MethodHandlerInvocation:
    ws: Any
    method: str
    req_id: str
    params: dict[str, Any]
    session_id: str
    handler: MethodHandler


class WebRpcHost:
    """Shared business host for WS and HTTP transports."""

    def __init__(self, channel: Any) -> None:
        self._channel = channel
        self._method_handlers: dict[str, MethodHandler] = {}
        self._on_message_cb: Callable[[Any], Any] | None = None
        self._connect_hooks: list[ConnectHook] = []
        self._disconnect_hooks: list[ConnectHook] = []
        self._history_runner: Any = None

    @property
    def method_handlers(self) -> dict[str, MethodHandler]:
        return self._method_handlers

    @property
    def on_message_cb(self) -> Callable[[Any], Any] | None:
        return self._on_message_cb

    @on_message_cb.setter
    def on_message_cb(self, value: Callable[[Any], Any] | None) -> None:
        self._on_message_cb = value

    def register_method(self, method: str, handler: MethodHandler) -> None:
        self._method_handlers[method] = handler

    def on_connect(self, callback: ConnectHook) -> None:
        self._connect_hooks.append(callback)

    def on_disconnect(self, callback: ConnectHook) -> None:
        self._disconnect_hooks.append(callback)

    def on_message(self, callback: Callable[[Any], None]) -> None:
        self._on_message_cb = callback

    def wrap_message_callback(
        self,
        wrapper: Callable[[Callable[[Any], Any] | None, Any], Any],
    ) -> None:
        original = self._on_message_cb

        def wrapped(msg: Any) -> Any:
            return wrapper(original, msg)

        self._on_message_cb = wrapped

    @property
    def connect_hooks(self) -> list[ConnectHook]:
        return self._connect_hooks

    @property
    def disconnect_hooks(self) -> list[ConnectHook]:
        return self._disconnect_hooks

    def maybe_start_history_capture(self) -> None:
        """Enterprise: capture frames on Listen for ``GET /api/sessions*``."""
        if self._history_runner is not None:
            return
        if not is_enterprise():
            return
        try:
            from jiuwenswarm.channels.web.history_store import (
                ChatHistoryStore,
                HistoryFrameRunner,
                set_default_store,
            )

            store = ChatHistoryStore.from_env()
            set_default_store(store)
            if not store.available:
                logger.warning(
                    "WebChannel 会话历史不可用（MySQL 缺 WEB_DB_HOST 或库类型不支持）",
                )
                return
            runner = HistoryFrameRunner(store)
            runner.start()
            self._history_runner = runner
            if store.mysql_settings is not None:
                s = store.mysql_settings
                logger.info(
                    "WebChannel enterprise history capture: %s %s:%s/%s%s",
                    store.backend, s.host, s.port, s.database,
                    f" schema={s.pg_schema}" if store.backend == "postgresql" else "",
                )
            else:
                logger.info(
                    "WebChannel enterprise history capture: %s",
                    store.backend,
                )
        except Exception:
            logger.warning("WebChannel 启动会话历史采集失败", exc_info=True)

    def shutdown(self) -> None:
        runner = self._history_runner
        if runner is None:
            return
        try:
            runner.stop()
        except Exception:  # noqa: BLE001
            logger.debug("WebChannel history runner stop failed", exc_info=True)
        self._history_runner = None

    def record_history_frame(self, direction: str, data: Any) -> None:
        runner = self._history_runner
        if runner is None or data is None:
            return
        try:
            if isinstance(data, bytes):
                raw = data.decode("utf-8", errors="replace")
            elif isinstance(data, dict):
                raw = json.dumps(data, ensure_ascii=False)
            else:
                raw = str(data)
            runner.submit(direction, raw)
        except Exception:
            logger.debug("WebChannel history submit failed dir=%s", direction, exc_info=True)

    async def invoke_method_handler(self, invocation: MethodHandlerInvocation) -> bool:
        ws_transport = self._channel.ws
        kwargs: dict[str, Any] = {}
        if "user_id" in inspect.signature(invocation.handler).parameters:
            kwargs["user_id"] = ws_transport.connection_user_id(invocation.ws)
        try:
            await invocation.handler(
                invocation.ws,
                invocation.req_id,
                invocation.params,
                invocation.session_id,
                **kwargs,
            )
            return True
        except Exception as e:
            ws_closed = bool(getattr(invocation.ws, "closed", False))
            if ws_closed:
                logger.warning(
                    "WebChannel method handler aborted on closed websocket: %s",
                    format_ws_diagnostics(
                        {
                            "method": invocation.method,
                            "id": invocation.req_id,
                            "session_id": invocation.session_id,
                        },
                        describe_ws_peer(invocation.ws),
                        describe_ws_exception(e),
                    ),
                )
                return False

            logger.error(
                "WebChannel method handler error: %s",
                format_ws_diagnostics(
                    {
                        "method": invocation.method,
                        "id": invocation.req_id,
                        "session_id": invocation.session_id,
                    },
                    describe_ws_peer(invocation.ws),
                    describe_ws_exception(e),
                ),
            )
            try:
                await self._channel.send_response(
                    invocation.ws, invocation.req_id, ok=False,
                    error=f"handler error: {e}", code="INTERNAL_ERROR",
                )
            except Exception as send_err:
                logger.warning(
                    "WebChannel failed to send handler error response ({}): {}",
                    invocation.method, send_err,
                )
            return False

    async def download_file(self, url: str) -> bytes | None:
        if not _is_url_safe_for_fetch(url):
            logger.warning("WebChannel blocked unsafe file URL: %s", url)
            return None
        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=True, max_redirects=3) as response:
                    if response.status != 200:
                        logger.warning(
                            "WebChannel 文件下载失败: %s, 状态码: %s", url, response.status,
                        )
                        return None
                    content = await response.read()
                    if len(content) > _MAX_DOWNLOAD_BYTES:
                        logger.warning(
                            "WebChannel 文件下载过大: %s, bytes=%s", url, len(content),
                        )
                        return None
                    return content
        except Exception as e:
            logger.warning("WebChannel 文件下载异常: %s, 错误: %s", url, e)
            return None

    async def process_files(self, params: dict[str, Any]) -> dict[str, Any]:
        files = params.get("files")
        if not files or not isinstance(files, list):
            return params

        strip_path_for_url = is_enterprise()
        downloaded_files = []
        workspace = Path(get_agent_workspace_dir()).resolve()

        for file_info in files:
            if not isinstance(file_info, dict):
                downloaded_files.append(file_info)
                continue

            file_url = file_info.get("url") or file_info.get("uri") or ""
            file_name = safe_filename(
                str(file_info.get("name") or file_info.get("filename") or "unknown_file"),
                default="unknown_file",
            )

            if file_url and strip_path_for_url:
                updated = dict(file_info)
                updated["url"] = file_url
                updated.pop("path", None)
                downloaded_files.append(updated)
                continue

            if file_url:
                file_content = await self.download_file(file_url)
                if file_content:
                    try:
                        workspace.mkdir(parents=True, exist_ok=True)
                        file_path = (workspace / file_name).resolve()
                        if not is_path_under_directory(workspace, file_path):
                            logger.warning(
                                "WebChannel blocked unsafe attachment path: %s", file_name,
                            )
                            downloaded_files.append(file_info)
                            continue
                        file_path.write_bytes(file_content)
                        file_info["path"] = str(file_path)
                    except Exception as e:
                        logger.warning("WebChannel 文件保存失败: %s", e)

            downloaded_files.append(file_info)

        params["files"] = downloaded_files
        return params
