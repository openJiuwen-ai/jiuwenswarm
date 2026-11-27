# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cloud plugin WebSocket client for invoking cloud plugins via agent-runtime-service."""

from __future__ import annotations
import os
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from jiuwenclaw.agentserver.skilldev_agent.meta_tools.agent_runtime_client import (
    AgentRuntimeClient
)
from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import ExternalToolSpec

logger = logging.getLogger(__name__)

# 错误码映射
CLOUD_PLUGIN_ERRORS = {
    (401, "001000"): "鉴权失败",
    (500, "001002"): "服务内部错误",
}


@dataclass
class CloudPluginContext:
    """端侧上下文信息（DM 传入）。"""

    # session 信息
    session_id: str = ""
    interaction_id: int = 0
    message_name: str = ""
    device_id: str = ""

    # deviceInfo
    device_name: str = ""
    device_type: str = ""
    sys_version: str = ""

    # clientContext
    agent_id: str = ""
    agent_login_session_id: str = ""
    current_agent_attachment: list[Any] = field(default_factory=list)
    service_center_data: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def generate_mock_extra_info(
            *, session_id: str | None = None, agent_id: str | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """动态生成模拟的 extraInfo 结构。

        Args:
            session_id: 自定义 session_id，不传则自动生成
            agent_id: 自定义 agent_id，不传则自动生成
            device_id: 自定义 device_id，不传则自动生成

        Returns:
            extraInfo 结构字典
        """
        import random
        import string
        from datetime import datetime, timezone

        if not session_id:
            # 模拟：sessionId: caojian + yyyyMMdd + 4位随机数字（未传入时使用默认逻辑）
            today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
            random_suffix = "".join(random.choices(string.digits, k=4))
            session_id = f"caojian{today}{random_suffix}"

        if not device_id:
            # 模拟：deviceId: EDSJIO + 7位随机大写字母数字（未传入时使用默认逻辑）
            device_suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=7))
            device_id = f"EDSJIO{device_suffix}"

        if not agent_id:
            # 模拟：agentId: agent + 32位随机十六进制（未传入时使用默认逻辑）
            agent_suffix = "".join(random.choices("0123456789abcdef", k=32))
            agent_id = f"agent{agent_suffix}"

        return {
            "session": {
                "sessionId": session_id,
                "interactionId": 2,
                "messageName": "startRecognize",
                "deviceId": device_id,
            },
            "context": {
                "deviceInfo": {
                    "deviceName": "phone",
                    "deviceType": "NOH-AN00",
                    "sysVersion": "OpenHarmony-5.0.2.42(Canary1)",
                },
                "clientContext": {
                    "agentId": agent_id,
                    "agentLoginSessionId": "".join(random.choices(string.digits, k=4)),
                    "currentAgentAttachment": [],
                    "serviceCenterData": [
                        {
                            "featureType": "CONTENT_CARD",
                            "featureVersion": "999.2",
                        },
                        {
                            "featureType": "CONTENT_CARD_SDK",
                            "featureVersion": "1.0",
                        }
                    ]
                }
            }
        }

    def to_extra_info(self) -> dict[str, Any]:
        """构造 extraInfo 结构。"""
        return {
            "session": {
                "sessionId": self.session_id,
                "interactionId": self.interaction_id,
                "messageName": self.message_name,
                "deviceId": self.device_id,
            },
            "context": {
                "deviceInfo": {
                    "deviceName": self.device_name,
                    "deviceType": self.device_type,
                    "sysVersion": self.sys_version,
                },
                "clientContext": {
                    "agentId": self.agent_id,
                    "agentLoginSessionId": self.agent_login_session_id,
                    "currentAgentAttachment": self.current_agent_attachment,
                    "serviceCenterData": self.service_center_data,
                },
            },
        }


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
            base_url: str = os.environ.get("AGENT_RUNTIME_MCP_RUN", ""),
            session_id: str | None = None,
            *,
            timeout: float = float(os.getenv("AGENT_RUNTIME_WS_TIMEOUT", "10.0")),
    ) -> None:
        """Initialize cloud plugin client."""
        self.session_id = session_id or ""
        # 单次插件调用的 sessionId
        self.plugin_session_id: str = f"plugin{uuid.uuid4().hex}"
        super().__init__(base_url, timeout=timeout)

    @staticmethod
    def final_response(frames, spec):
        # 合并text帧内容
        contents = [
            f.get("content", "")
            for f in frames
            if f.get("success") and f.get("content") and f.get("event", "") in {"text", "command"}
        ]
        final_content = "".join(contents)
        # 检查是否有错误帧
        error_frames = [f for f in frames if not f.get("success")]
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
        """流式帧终止判定：event == "finish" 视为最后一帧。"""
        if not isinstance(frame, dict):
            return False
        event = frame.get("event", "")
        if isinstance(event, str):
            return event.strip().lower() == "finish"
        return False

    @staticmethod
    def _build_request_body(
            spec: ExternalToolSpec,
            arguments: dict[str, Any],
            context: CloudPluginContext | None = None,
            **kwargs: Any
    ) -> dict[str, Any]:
        """构造请求体。"""
        body: dict[str, Any] = {
            "bundleName": spec.plugin_id,
            "functionName": spec.tool_name,
            "arguments": arguments,
        }
        # Agent ID
        agent_id = None
        if spec.tool_name == "agent_as_a_tool":
            agent_id = str(arguments.get("agentId") or arguments.get("agent_id") or "").strip()
        # 模拟字段逻辑：无 context 时动态生成
        if not context:
            extra_info = CloudPluginContext.generate_mock_extra_info(
                agent_id=agent_id,
                session_id=kwargs.get("session_id")
            )
            body["extraInfo"] = extra_info
        else:
            body["extraInfo"] = context.to_extra_info()
        return body

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
        """调用云插件（单次响应，等待 finish 事件）。

        Args:
            spec: ExternalToolSpec
            arguments: 调用参数
            context: 端侧上下文信息（DM 传入）

        Returns:
            响应结果 dict，包含 success、content、errCode 等字段
        """
        plugin_id, tool_name = spec.plugin_id, spec.tool_name
        body = self._build_request_body(spec, arguments, session_id=self.plugin_session_id)
        url = self._base_url
        headers = self._build_headers()
        message = json.dumps(body, ensure_ascii=False)

        logger.debug(
            "[session=%s] [%s] [CloudPluginClient] invoke pluginId=%s toolName=%s url=%s",
            self.session_id, self.plugin_session_id, plugin_id, tool_name, url
        )

        try:
            ws_ctx = await self._connect_with_retry(url, headers)
        except Exception as e:
            logger.error(
                "[session=%s] [%s] [CloudPluginClient] WS connect failed after retries: %s",
                self.session_id, self.plugin_session_id, e
            )
            return self._build_error_response(spec, f"WebSocket 连接失败: {e}")

        logger.info(
            "[session=%s] [%s] [CloudPluginClient] pluginId=%s toolName=%s WS connect succeed",
            self.session_id, self.plugin_session_id, plugin_id, tool_name
        )

        # 接收帧并返回结果
        frames = await self._receive_frames(ws_ctx, message, spec)
        rsp = self.final_response(frames, spec)
        logger.info(
            "[session=%s] [%s] [CloudPluginClient] pluginId=%s toolName=%s final processing result: %s",
            self.session_id, self.plugin_session_id, plugin_id, tool_name, rsp
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
                await ws.send(message)
                logger.debug(
                    "[session=%s] [%s] [CloudPluginClient] Send message: %s",
                    self.session_id, self.plugin_session_id, message
                )

                while True:
                    raw = await self._recv_single_frame(ws)
                    if raw is None:  # 接收超时
                        logger.warning(
                            "[session=%s] [%s] [CloudPluginClient] WS recv timeout (%ss)",
                            self.session_id, self.plugin_session_id, self._timeout,
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
                "[session=%s] [%s] [CloudPluginClient] WS cancelled",
                self.session_id,
                self.plugin_session_id
            )
            frames.append(self._build_error_frame(spec, "WebSocket 连接任务被取消"))
        except asyncio.TimeoutError:
            logger.error(
                "[session=%s] [%s] [CloudPluginClient] WS timeout",
                self.session_id,
                self.plugin_session_id
            )
            frames.append(self._build_error_frame(spec, "WebSocket 连接超时"))
        except Exception as e:
            logger.error(
                "[session=%s] [%s] [CloudPluginClient] WS operation failed: %s",
                self.session_id,
                self.plugin_session_id, e, exc_info=True
            )
            frames.append(self._build_error_frame(spec, f"WebSocket 消息接收循环异常退出: {e}"))

        return frames

    async def _recv_single_frame(self, ws: Any) -> str | None:
        """接收单个帧，超时返回 None。"""
        try:
            # 单次接收消息时延 10 秒，服务方中间帧为 8s
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

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        from jiuwenclaw.agentserver.open_ability_utils import get_oa_auth_headers
        headers = get_oa_auth_headers(session_id=self.plugin_session_id)
        headers["x-uid"] = "x-uid placeholder"
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
