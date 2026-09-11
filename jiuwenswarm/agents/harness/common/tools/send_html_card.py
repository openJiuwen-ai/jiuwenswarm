# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Send HTML Card Toolkit

以 H5 卡片形式将 HTML 页面投递给用户（对齐 openclaw send_html_card）。

使用方式：
1. 创建 SendHtmlCardToolkit 实例
2. 调用 get_tools() 获取工具列表
3. 工具会自动注册到 Runner 中
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, List

import aiohttp
from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.file_upload_helpers import (
    XiaoyiObsUploadConfig,
    upload_local_file_public_url,
)

logger = logging.getLogger(__name__)

_DEFAULT_PREVIEW_EXPIRE_SECONDS = 259200


def _get_obs_config() -> XiaoyiObsUploadConfig:
    """从配置中读取 OBS 上传配置."""
    from jiuwenswarm.common.config import get_config

    cfg = get_config()
    xc = cfg.get("channels", {}).get("xiaoyi", {})
    base = xc.get("file_upload_url")
    api_key = xc.get("api_key")
    uid = str(xc.get("uid") or "")
    if not base or not api_key or not uid:
        raise RuntimeError(
            "Missing channels.xiaoyi config: file_upload_url / api_key / uid; "
            "cannot upload HTML"
        )
    return XiaoyiObsUploadConfig(base_url=base, api_key=api_key, uid=uid)


class SendHtmlCardToolkit:
    """Toolkit for sending HTML content as an H5 card to the user."""

    def __init__(
        self,
        request_id: str,
        session_id: str,
        channel_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None
        logger.debug(
            "[SendHtmlCardToolkit] 初始化 request_id=%s session_id=%s "
            "channel_id=%s has_metadata=%s",
            request_id,
            session_id,
            channel_id,
            bool(self._request_metadata),
        )

    def update_runtime_context(
        self,
        *,
        request_id: str,
        session_id: str,
        channel_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update per-request runtime context without recreating the toolkit/tool."""
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None
        logger.debug(
            "[SendHtmlCardToolkit] update_runtime_context request_id=%s "
            "session_id=%s channel_id=%s has_metadata=%s",
            request_id,
            session_id,
            channel_id,
            bool(self._request_metadata),
        )

    async def _resolve_html_url(
        self,
        html_url: str | None,
        html_local: str | None,
    ) -> str:
        url = (html_url or "").strip() or None
        local = (html_local or "").strip() or None

        if local:
            if not os.path.isfile(local):
                raise RuntimeError(f"Local HTML file does not exist: {local}")
            obs_cfg = _get_obs_config()
            async with aiohttp.ClientSession() as session:
                url = await upload_local_file_public_url(
                    session,
                    obs_cfg,
                    local,
                    need_preview=True,
                    expire_time=_DEFAULT_PREVIEW_EXPIRE_SECONDS,
                )
            logger.info(
                "[SendHtmlCardToolkit] uploaded html_local=%s preview_url_len=%s",
                local,
                len(url or ""),
            )

        if not url:
            raise RuntimeError("Failed to obtain the HTML page URL")
        return url

    async def send_html_card(
        self,
        html_url: str | None = None,
        html_local: str | None = None,
        **_ignored: Any,
    ) -> str:
        """Send HTML as an H5 card to the user.

        Args:
            html_url: Publicly accessible HTML page URL.
            html_local: Absolute path to a local HTML file (uploaded for preview).

        Returns:
            Success message including the full preview URL, or an error string.
        """
        if not (html_url or "").strip() and not (html_local or "").strip():
            return "Failed to send HTML card: at least one of html_url and html_local is required"

        try:
            url = await self._resolve_html_url(html_url, html_local)
        except Exception as e:
            logger.exception(
                "[SendHtmlCardToolkit] resolve url failed session_id=%s error=%s",
                self.session_id,
                e,
            )
            return f"Failed to send HTML card: {e}"

        cards_info = [
            {
                "cardName": "clawH5",
                "cardData": {"url": url},
                "displayType": "DisplayFaCard",
            }
        ]

        try:
            from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
            from jiuwenswarm.server.runtime.session.session_history import (
                append_history_record,
            )

            append_history_record(
                session_id=self.session_id,
                request_id=self.request_id,
                channel_id=self.channel_id,
                role="assistant",
                event_type="chat.html_card",
                content="",
                timestamp=time.time(),
                extra={"cardsInfo": cards_info, "url": url},
            )

            msg: dict[str, Any] = {
                "request_id": self.request_id,
                "channel_id": self.channel_id,
                "session_id": self.session_id,
                "payload": {
                    "event_type": "chat.html_card",
                    "cardsInfo": cards_info,
                    "url": url,
                },
                "is_complete": False,
            }
            if self._request_metadata:
                msg["metadata"] = dict(self._request_metadata)

            server = AgentWebSocketServer.get_instance()
            await server.send_push(msg)
            logger.info(
                "[SendHtmlCardToolkit] send_push ok session_id=%s url_len=%s",
                self.session_id,
                len(url),
            )
            return (
                "HTML card sent successfully. The public URL of the HTML page is below; "
                f"when generating a markdown hyperlink, keep it fully identical to this url {url}"
            )
        except Exception as e:
            logger.exception(
                "[SendHtmlCardToolkit] send_html_card 失败 session_id=%s error=%s",
                self.session_id,
                e,
            )
            return f"Failed to submit HTML card: {e}"

    def get_tools(self) -> List[Tool]:
        """Return tools for registration in Runner."""

        def make_tool(
            name: str,
            description: str,
            input_params: dict,
            func,
        ) -> Tool:
            card = ToolCard(
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="send_html_card",
                description=(
                    "[HTML card sending tool] Presents HTML page content as an H5 card; "
                    "the user can view it directly inside the card. "
                    "Prefer this tool when sending HTML to the user; use send_file_to_user "
                    "only when the user explicitly asks for the raw file. "
                    "Parameters: at least one of html_url and html_local is required; "
                    "html_url is a publicly accessible HTML address; "
                    "html_local is an absolute path to a local HTML file (uploaded first "
                    "to obtain a preview link). "
                    "Call this tool only when the user or a skill explicitly says to use "
                    "send_html_card. "
                    "The public URL in the tool result must be preserved in full "
                    "(including auth parameters); do not truncate it when generating "
                    "markdown hyperlinks."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "html_url": {
                            "type": "string",
                            "description": "Online HTML page link: a URL directly accessible from the public internet",
                        },
                        "html_local": {
                            "type": "string",
                            "description": "Absolute path to a local HTML file",
                        },
                    },
                    "required": [],
                },
                func=self.send_html_card,
            ),
        ]
