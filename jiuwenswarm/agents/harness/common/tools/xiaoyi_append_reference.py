# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Xiaoyi append-reference toolkit.

对齐 OpenClaw xiaoyi_append_reference：把联网搜索等引用来源作为独立
``chat.reference`` 事件发给当前客户端。手机侧由 Gateway 转为 A2A
data.reference；桌面侧由前端消费正式事件，旧前端仍可通过工具结果兜底。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, List

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.common.xiaoyi_reference import coerce_references
from jiuwenswarm.agents.harness.common.tools.turn_request_identity import (
    resolve_toolkit_delivery,
)

logger = logging.getLogger(__name__)


class XiaoyiAppendReferenceToolkit:
    """Deliver citations through E2A for desktop and Xiaoyi clients."""

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
            return json.dumps(
                {
                    "success": False,
                    "count": 0,
                    "message": (
                        "Failed to send references: references must be a non-empty "
                        "array, and each item must contain title/url/source/name"
                    ),
                    "reference_event_sent": False,
                    "history_saved": False,
                    "references": [],
                },
                ensure_ascii=False,
            )

        history_saved = False
        event_sent = False
        errors: list[str] = []
        delivery = None
        try:
            from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
            from jiuwenswarm.server.runtime.session.session_history import (
                append_history_record,
            )

            delivery = resolve_toolkit_delivery(self)
        except Exception as exc:
            errors.append(f"delivery setup failed: {exc}")
            logger.exception(
                "[XiaoyiAppendReference] 投递上下文解析失败 session_id=%s error=%s",
                self.session_id,
                exc,
            )

        if delivery is not None:
            try:
                history_saved = bool(
                    await asyncio.to_thread(
                        append_history_record,
                        session_id=delivery.session_id,
                        request_id=delivery.turn_request_id,
                        channel_id=delivery.channel_id,
                        role="assistant",
                        event_type="chat.reference",
                        content="",
                        timestamp=time.time(),
                        extra={"references": items},
                        wait=True,
                    )
                )
                if not history_saved:
                    errors.append("history write skipped")
            except Exception as exc:
                errors.append(f"history write failed: {exc}")
                logger.exception(
                    "[XiaoyiAppendReference] 历史保存失败 session_id=%s error=%s",
                    self.session_id,
                    exc,
                )

        if delivery is not None:
            try:
                msg: dict[str, Any] = {
                    "request_id": delivery.turn_request_id,
                    "channel_id": delivery.channel_id,
                    "session_id": delivery.session_id,
                    "payload": {
                        "event_type": "chat.reference",
                        "references": items,
                    },
                    "is_complete": False,
                }
                if delivery.metadata:
                    msg["metadata"] = dict(delivery.metadata)

                server = AgentWebSocketServer.get_instance()
                await server.send_push(msg)
                event_sent = True
                logger.info(
                    "[XiaoyiAppendReference] send_push ok session_id=%s channel=%s count=%s",
                    self.session_id,
                    delivery.channel_id,
                    len(items),
                )
            except Exception as exc:
                errors.append(f"live push failed: {exc}")
                logger.exception(
                    "[XiaoyiAppendReference] 实时推送失败 session_id=%s error=%s",
                    self.session_id,
                    exc,
                )

        # Legacy PC frontends do not consume chat.reference yet. Returning the
        # page URLs in the tool result keeps them visible through the existing
        # extractPanelData() heuristic. imageUrl is intentionally omitted so it
        # is not mistaken for another cited page by that fallback.
        fallback_items = [
            {key: value for key, value in item.items() if key != "imageUrl"}
            for item in items
        ]
        result: dict[str, Any] = {
            # A persisted reference is usable through history/legacy fallback even
            # when the live push fails. Keep the two delivery channels explicit so
            # the model does not retry and create duplicate history records.
            "success": history_saved or event_sent,
            "count": len(items),
            "message": (
                f"Sent {len(items)} references"
                if event_sent
                else (
                    f"Saved {len(items)} references; live delivery failed"
                    if history_saved
                    else "Failed to submit references"
                )
            ),
            "reference_event_sent": event_sent,
            "history_saved": history_saved,
            "references": fallback_items,
        }
        if errors:
            result["error"] = "; ".join(errors)
        return json.dumps(result, ensure_ascii=False)

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
                    "answer (e.g. web search) to the user as a reference section or "
                    "cards on the current desktop or mobile client. When the conversation involves web search, or "
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
