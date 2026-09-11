# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Xiaoyi append-reference toolkit.

对齐 OpenClaw xiaoyi_append_reference：把联网搜索等引用来源打给手机端
（A2A data.reference 卡片）。仅 xiaoyi 渠道注册。
"""

from __future__ import annotations

import logging
import time
from typing import Any, List

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.common.xiaoyi_reference import coerce_references

logger = logging.getLogger(__name__)


class XiaoyiAppendReferenceToolkit:
    """Push citation cards to the Xiaoyi phone client."""

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

    def update_runtime_context(
        self,
        *,
        request_id: str,
        session_id: str,
        channel_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None

    async def append_reference(self, references: Any = None, **_ignored: Any) -> str:
        items = coerce_references(references)
        if not items:
            return "Failed to send references: references must be a non-empty array, and each item must contain title/url/source/name"

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
                event_type="chat.reference",
                content="",
                timestamp=time.time(),
                extra={"references": items},
            )

            msg: dict[str, Any] = {
                "request_id": self.request_id,
                "channel_id": self.channel_id,
                "session_id": self.session_id,
                "payload": {
                    "event_type": "chat.reference",
                    "references": items,
                },
                "is_complete": False,
            }
            if self._request_metadata:
                msg["metadata"] = dict(self._request_metadata)

            server = AgentWebSocketServer.get_instance()
            await server.send_push(msg)
            logger.info(
                "[XiaoyiAppendReference] send_push ok session_id=%s count=%s",
                self.session_id,
                len(items),
            )
            return f"Sent {len(items)} references"
        except Exception as e:
            logger.exception(
                "[XiaoyiAppendReference] 失败 session_id=%s error=%s",
                self.session_id,
                e,
            )
            return f"Failed to submit references: {e}"

    def get_tools(self) -> List[Tool]:
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
                name="xiaoyi_append_reference",
                description=(
                    "[Reference sources] Return information sources found during the "
                    "answer (e.g. web search) to the user as reference cards on the "
                    "mobile client. When the conversation involves web search, or "
                    "depends on data from query skills such as weather / finance / "
                    "health / encyclopedia, you MUST call this tool exactly once when "
                    "composing the final merged answer; do not call it multiple times. "
                    "For example, results from web-search tools such as xiaoyi-web-search "
                    "must be delivered as reference sources. Calling this tool does not "
                    "interrupt the current streaming output. "
                    "title is the page title, name is the site name (e.g. Baidu Baike), "
                    "source is the source type (e.g. web_search), url is the clickable link."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "references": {
                            "type": "array",
                            "description": "Array of reference items, each element is one reference object",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {
                                        "type": "string",
                                        "description": "Title of the referenced page, shown as the card's main title",
                                    },
                                    "url": {
                                        "type": "string",
                                        "description": "Link address of the referenced page, used for click-through",
                                    },
                                    "source": {
                                        "type": "string",
                                        "description": (
                                            "Source type identifier, e.g. web_search, document, knowledge_base"
                                        ),
                                    },
                                    "name": {
                                        "type": "string",
                                        "description": "Site name, e.g. Baidu Baike, Wikipedia",
                                    },
                                    "imageUrl": {
                                        "type": "string",
                                        "description": "Small logo icon URL for the page title, optional",
                                    },
                                },
                                "required": ["title", "url", "source", "name"],
                            },
                        },
                    },
                    "required": ["references"],
                },
                func=self.append_reference,
            ),
        ]
