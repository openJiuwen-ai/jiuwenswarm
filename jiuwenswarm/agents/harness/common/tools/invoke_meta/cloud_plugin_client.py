# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cloud plugin WebSocket client for invoking cloud plugins via agent-runtime-service."""

from __future__ import annotations
import os
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.agents.harness.common.tools.invoke_meta.agent_runtime_client import (
    AgentRuntimeClient,
)
from jiuwenswarm.agents.harness.common.tools.invoke_meta.external_tool_registry import (
    ExternalToolSpec,
)

logger = logging.getLogger(__name__)

# 错误码映射
CLOUD_PLUGIN_ERRORS = {
    (401, "001000"): "鉴权失败",
    (500, "001002"): "服务内部错误",
}


def _needs_insecure_ssl(url: str) -> bool:
    """Skip TLS verify for test-domain WSS and raw IP.

    Same host rules as desktop isInsecureHost. mcp/run is a direct Python
    websockets connection (not via Electron), so this process must decide again.
    """
    import re
    from urllib.parse import urlparse

    if not (url or "").startswith("wss://"):
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return True
    if ":" in host.strip("[]"):
        return True
    return host == "hwcloudtest.cn" or host.endswith(".hwcloudtest.cn")


def _insecure_ssl():
    import ssl

    ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@dataclass
class CloudPluginContext:
    """设备/会话上下文（桌面 env 或 invocation；缺省 PC）。"""

    session_id: str = ""
    interaction_id: int = 0
    device_id: str = ""
    device_name: str = ""
    device_type: str = ""
    sys_version: str = ""

    def to_extra_info(self) -> dict[str, Any]:
        """构造 extraInfo（设备/会话上下文）。"""
        from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
            build_plugin_skill_extra_info,
        )

        base = build_plugin_skill_extra_info(
            session_id=self.session_id,
            interaction_id=self.interaction_id,
        )
        # Prefer explicit context fields when provided on the dataclass.
        if self.device_id:
            base["session"]["deviceId"] = self.device_id
            base["context"]["deviceInfo"]["x-device-id"] = self.device_id
        if self.device_name:
            base["context"]["deviceInfo"]["deviceName"] = self.device_name
        if self.device_type:
            base["context"]["deviceInfo"]["x-device-type"] = self.device_type
        if self.sys_version:
            base["context"]["deviceInfo"]["sysVersion"] = self.sys_version
        return base


def _map_error_code(status_code: int | None, err_code: str | None) -> str:
    """映射错误码到错误消息。"""
    if status_code is not None and err_code:
        key = (status_code, err_code)
        if key in CLOUD_PLUGIN_ERRORS:
            return CLOUD_PLUGIN_ERRORS[key]
    if err_code:
        return f"错误码: {err_code}"
    return "未知错误"


class CloudPluginClient(AgentRuntimeClient):
    """Client for cloud plugin WebSocket endpoints."""

    def __init__(
            self,
            base_url: str = "",
            session_id: str | None = None,
            *,
            timeout: float | None = None,
    ) -> None:
        from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
            resolve_plugin_runtime_url,
        )

        resolved = (base_url or "").strip() or resolve_plugin_runtime_url()
        self.session_id = session_id or ""
        # 单次插件调用的 sessionId
        self.plugin_session_id: str = f"plugin{uuid.uuid4().hex}"
        if timeout is None:
            timeout = float(os.getenv("AGENT_RUNTIME_WS_TIMEOUT", "120.0") or "120.0")
        super().__init__(resolved, timeout=timeout)

    @staticmethod
    def final_response(frames, spec):
        # 合并 text 帧；无 success 字段时以 event 为准
        contents = []
        for f in frames:
            event = str(f.get("event", "") or "")
            content = f.get("content", "")
            if not content:
                continue
            if f.get("success") is False:
                continue
            if event in {"text", "command"} or (not event and content):
                contents.append(content)
        final_content = "".join(contents)
        # 检查是否有错误帧
        error_frames = [f for f in frames if f.get("success") is False]
        if error_frames:
            first_error = error_frames[0]
            mm = first_error.get("mappedMessage", "")
            em = first_error.get("errMessage", "云插件调用失败")
            if mm:
                error = f"{mm}; {em}"
            else:
                error = em
            return {
                "success": False,
                "error": error,
                "content": final_content,
                "pluginId": spec.plugin_id,
                "toolName": spec.tool_name,
                "pluginType": spec.plugin_type
            }
        return {
            "success": True,
            "content": final_content,
            "pluginId": spec.plugin_id,
            "toolName": spec.tool_name,
            "pluginType": spec.plugin_type
        }

    @staticmethod
    def _parse_cloud_response(frame: dict[str, Any]) -> dict[str, Any]:
        """解析云插件响应帧，处理正常/异常响应。"""
        event = frame.get("event", "")
        resp_type = frame.get("type", "")

        # 异常响应
        if resp_type == "abnormal":
            err_code = frame.get("errCode", "")
            err_msg = frame.get("errMessage", "")
            mapped_msg = _map_error_code(None, err_code)
            return {
                "success": False,
                "event": event,
                "type": resp_type,
                "errCode": err_code,
                "errMessage": err_msg,
                "mappedMessage": mapped_msg,
            }

        if event == "finish":
            # 正常结束
            return {
                "success": True,
                "content": frame.get("content", ""),
                "type": resp_type,
                "event": event,
            }

        if event == "command":
            return CloudPluginClient._parse_command_frame(frame, resp_type)

        # 正常响应（text/directives_heartbeat/turnContinue 等）
        return {
            "success": True,
            "content": frame.get("content", ""),
            "type": resp_type,
            "event": event,
        }

    @staticmethod
    def _parse_command_frame(frame: dict[str, Any], resp_type: str) -> dict[str, Any]:
        """解析 command 事件帧，提取 directives 中的文本内容。

        Args:
            frame: 原始帧数据
            resp_type: 响应类型

        Returns:
            解析后的帧数据，包含提取的 text 字段
        """
        raw_content = frame.get("content", "{}")
        text_parts: list[str] = []

        try:
            content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
            directives = content.get("directives", [])

            for directive in directives:
                name = directive.get("header", {}).get("name", "")
                payload = directive.get("payload", {})

                # 提取 StreamingSpeak 的 text
                if name == "StreamingSpeak":
                    speak_text = payload.get("text", "")
                    if speak_text:
                        text_parts.append(speak_text)

        except json.JSONDecodeError:
            logger.warning(
                "[CloudPluginClient] Failed to parse command frame content as JSON: %s",
                raw_content[:200]
            )

        return {
            "success": True,
            "content": "".join(text_parts),
            "raw_content": raw_content,
            "type": resp_type,
            "event": "command",
        }

    @staticmethod
    def _is_final_frame(frame: dict[str, Any]) -> bool:
        """流式终止：event=finish，或 text 帧 content.streamInfo.streamType=final。"""
        if not isinstance(frame, dict):
            return False
        event = frame.get("event", "")
        if isinstance(event, str) and event.strip().lower() == "finish":
            return True
        if isinstance(event, str) and event.strip().lower() == "text":
            raw = frame.get("content", "")
            try:
                content = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError):
                return False
            if isinstance(content, dict):
                stream_info = content.get("streamInfo") or {}
                if isinstance(stream_info, dict):
                    return str(stream_info.get("streamType") or "").strip().lower() == "final"
        return False

    @staticmethod
    def _build_request_body(
            spec: ExternalToolSpec,
            arguments: dict[str, Any],
            context: CloudPluginContext | None = None,
            **kwargs: Any
    ) -> dict[str, Any]:
        """构造请求体（extraInfo + functionName/arguments）。"""
        from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
            build_plugin_skill_extra_info,
        )

        # arguments 内再带一份 bundleName/functionName
        call_args = dict(arguments)
        call_args.setdefault("bundleName", spec.plugin_id)
        call_args.setdefault("functionName", spec.tool_name)

        if context is not None:
            extra_info = context.to_extra_info()
        else:
            extra_info = build_plugin_skill_extra_info(
                session_id=kwargs.get("session_id"),
            )

        return {
            "extraInfo": extra_info,
            "bundleName": spec.plugin_id,
            "skillName": str(call_args.get("skillName") or ""),
            "functionName": spec.tool_name,
            "arguments": call_args,
            "turnContinue": bool(call_args.get("turnContinue", False)),
            "eventContexts": call_args.get("eventContexts", None),
            "progressToken": str(call_args.get("progressToken") or ""),
            "contexts": call_args.get("contexts", None),
        }

    @staticmethod
    def _build_error_frame(spec: ExternalToolSpec, err_msg: str) -> dict[str, Any]:
        """构建错误帧。"""
        return {
            "success": False,
            "errMessage": err_msg,
            "event": "finish",
            "type": "abnormal",
            "pluginId": spec.plugin_id,
            "toolName": spec.tool_name,
            "pluginType": spec.plugin_type
        }

    @staticmethod
    def _build_error_response(spec: ExternalToolSpec, error: str) -> dict[str, Any]:
        """构建错误响应。"""
        return {
            "success": False,
            "error": error,
            "pluginId": spec.plugin_id,
            "toolName": spec.tool_name,
            "pluginType": spec.plugin_type
        }

    async def invoke(
            self,
            spec: ExternalToolSpec,
            arguments: dict[str, Any],
            *,
            context: CloudPluginContext | None = None,
    ) -> dict[str, Any]:
        """调用云插件：收帧直到 finish 或失败，再合并为一次结果。

        Args:
            spec: ExternalToolSpec
            arguments: 调用参数
            context: 设备/会话上下文（桌面 env 或 invocation；缺省 PC）

        Returns:
            响应结果 dict，包含 success、content、errCode 等字段
        """
        plugin_id, tool_name = spec.plugin_id, spec.tool_name
        body = self._build_request_body(
            spec,
            arguments,
            context=context,
            session_id=self.plugin_session_id,
        )
        url = self._base_url
        headers = self._build_headers()
        message = json.dumps(body, ensure_ascii=False)

        logger.debug(
            "[session=%s] [%s] [CloudPluginClient] invoke pluginId=%s toolName=%s url=%s",
            self.session_id, self.plugin_session_id, plugin_id, tool_name, url
        )
        self._log_handshake_summary(url, headers, plugin_id, tool_name)

        try:
            ws_ctx = await self._connect_with_retry(url, headers)
        except Exception as e:
            self._log_handshake_reject(e, plugin_id, tool_name)
            logger.error(
                "[session=%s] [%s] [CloudPluginClient] WS connect failed after retries: %s",
                self.session_id, self.plugin_session_id, e
            )
            return self._build_error_response(spec, f"WebSocket 连接失败: {e}")

        # 接收帧并返回结果（handshake_ok 仅在 async with 真正握上之后）
        frames = await self._receive_frames(ws_ctx, message, spec)
        rsp = self.final_response(frames, spec)
        if rsp.get("success"):
            logger.info(
                "[session=%s] [%s] [CloudPluginClient] pluginId=%s toolName=%s success=True",
                self.session_id, self.plugin_session_id, plugin_id, tool_name,
            )
        else:
            logger.warning(
                "[session=%s] [%s] [CloudPluginClient] pluginId=%s toolName=%s success=False error=%s",
                self.session_id, self.plugin_session_id, plugin_id, tool_name,
                rsp.get("error") or "云插件调用失败",
            )
        return rsp

    async def _receive_frames(
            self,
            ws_ctx: Any,
            message: str,
            spec: ExternalToolSpec,
    ) -> list[dict[str, Any]]:
        """接收 WebSocket 帧直到 finish 或异常。

        Args:
            ws_ctx: WebSocket 连接上下文
            message: 要发送的消息
            spec: 工具规格

        Returns:
            帧列表
        """
        plugin_id, tool_name = spec.plugin_id, spec.tool_name
        frames: list[dict[str, Any]] = []

        try:
            async with ws_ctx as ws:
                logger.info(
                    "[session=%s] [%s] [CloudPluginClient] mcp/run handshake phase=handshake_ok "
                    "pluginId=%s functionName=%s",
                    self.session_id, self.plugin_session_id, plugin_id, tool_name,
                )
                await ws.send(message)

                while True:
                    raw = await self._recv_single_frame(ws)
                    if raw is None:  # 接收超时
                        logger.warning(
                            "[session=%s] [%s] [CloudPluginClient] WS recv timeout (%ss); "
                            "last request body=%s",
                            self.session_id,
                            self.plugin_session_id,
                            self._timeout,
                            message,
                        )
                        return [self._build_error_frame(spec, f"响应超时 ({self._timeout}s)")]

                    frame = self._parse_raw_frame(raw)
                    parsed = self._parse_cloud_response(frame)
                    logger.debug(
                        "[session=%s] [%s] [CloudPluginClient] pluginId=%s toolName=%s chunk: %s",
                        self.session_id, self.plugin_session_id, plugin_id, tool_name, str(parsed)
                    )
                    frames.append(parsed)

                    if not parsed.get("success") or self._is_final_frame(frame):
                        break

        except asyncio.CancelledError:
            logger.error(
                "[session=%s] [%s] [CloudPluginClient] WS cancelled; last request body=%s",
                self.session_id,
                self.plugin_session_id,
                message,
            )
            raise
        except asyncio.TimeoutError:
            logger.error(
                "[session=%s] [%s] [CloudPluginClient] WS timeout; last request body=%s",
                self.session_id,
                self.plugin_session_id,
                message,
            )
            frames.append(self._build_error_frame(spec, "WebSocket 连接超时"))
        except Exception as e:
            self._log_handshake_reject(e, plugin_id, tool_name)
            logger.error(
                "[session=%s] [%s] [CloudPluginClient] WS operation failed: %s; last request body=%s",
                self.session_id,
                self.plugin_session_id, e, message, exc_info=True
            )
            frames.append(self._build_error_frame(spec, f"WebSocket 消息接收循环异常退出: {e}"))

        return frames

    async def _recv_single_frame(self, ws: Any) -> str | None:
        """接收单个帧，超时返回 None。"""
        try:
            # 单帧 recv 超时为 self._timeout（默认 AGENT_RUNTIME_WS_TIMEOUT=120s；成曲 MUSIC_WS_TIMEOUT=600s）
            raw = await asyncio.wait_for(ws.recv(), timeout=self._timeout)
            if isinstance(raw, bytes):
                return raw.decode("utf-8")
            return raw
        except asyncio.TimeoutError:
            return None

    def _parse_raw_frame(self, raw: str) -> dict[str, Any]:
        """解析原始帧数据。"""
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "[session=%s] [%s] [CloudPluginClient] Invalid JSON response: %s",
                self.session_id,
                self.plugin_session_id,
                raw
            )
            return {
                "success": False,
                "event": "finish",
                "type": "abnormal",
                "errMessage": f"Invalid JSON response: {raw}"
            }

    def _log_handshake_summary(
            self,
            url: str,
            headers: dict[str, str],
            plugin_id: str,
            tool_name: str,
    ) -> None:
        from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
            handshake_cred_source,
            mask_secret,
        )

        logger.info(
            "[session=%s] [%s] [CloudPluginClient] mcp/run handshake "
            "url=%s pluginId=%s functionName=%s cred=%s credSrc=%s "
            "x-uid=%s x-device-id=%s x-hag-trace-id=%s",
            self.session_id,
            self.plugin_session_id,
            url,
            plugin_id,
            tool_name,
            mask_secret(headers.get("businessCredential") or ""),
            handshake_cred_source(url),
            headers.get("x-uid") or "",
            headers.get("x-device-id") or "",
            headers.get("x-hag-trace-id") or "",
        )

    def _log_handshake_reject(self, exc: BaseException, plugin_id: str, tool_name: str) -> None:
        from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
            format_masked_headers,
            handshake_reject_status_and_headers,
            is_handshake_reject,
        )

        status, headers = handshake_reject_status_and_headers(exc)
        if not is_handshake_reject(exc) and status is None:
            return
        logger.info(
            "[session=%s] [%s] [CloudPluginClient] mcp/run handshake phase=handshake_reject "
            "status=%s headers=%s pluginId=%s functionName=%s",
            self.session_id,
            self.plugin_session_id,
            status,
            format_masked_headers(headers),
            plugin_id,
            tool_name,
        )

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        from jiuwenswarm.agents.harness.common.tools.invoke_meta.useraccess_runtime import (
            build_runtime_headers,
        )

        headers = build_runtime_headers(
            extra={"x-plugin-session-id": self.plugin_session_id},
            url=self._base_url,
        )
        if extra:
            headers.update(extra)
        return headers

    def _build_connect_kwargs(
            self, headers: dict[str, str] | None
    ) -> dict[str, Any]:
        import websockets
        connect_kwargs: dict[str, Any] = {
            "open_timeout": self._timeout,
            "close_timeout": self._timeout
        }
        if headers:
            if self._connect is websockets.connect:
                connect_kwargs["additional_headers"] = headers
            else:
                connect_kwargs["extra_headers"] = headers
        if _needs_insecure_ssl(self._base_url):
            connect_kwargs["ssl"] = _insecure_ssl()
        return connect_kwargs

    async def _connect_with_retry(
            self, url: str, headers: dict[str, str]
    ) -> Any:
        """WS 连接重试逻辑，最大重试 3 次。"""
        _max_connect_retries = int(os.getenv("AGENT_RUNTIME_WS_CONNECT_RETRIES", 3))
        last_error: Exception | None = None
        for attempt in range(_max_connect_retries):
            try:
                connect_kwargs = self._build_connect_kwargs(headers)
                return self._connect(url, **connect_kwargs)
            except Exception as e:
                last_error = e
                logger.warning(
                    "[session=%s] [%s] [CloudPluginClient] WS connect attempt %d/%d failed: %s",
                    self.session_id, self.plugin_session_id, attempt + 1, _max_connect_retries, e,
                )
                if attempt < _max_connect_retries - 1:
                    # 递增：0.5s, 1s, 1.5s
                    await asyncio.sleep(0.5 * (attempt + 1))
        if last_error:
            raise last_error
        raise RuntimeError("WS connect failed after retries")
