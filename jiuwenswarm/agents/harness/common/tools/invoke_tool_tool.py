# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""invoke_tool: Invoke a deferred (on-demand) tool with validated arguments."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from pydantic import BaseModel, Field

from openjiuwen.core.foundation.tool import Tool, ToolCard

from jiuwenswarm.common.tool_ownership import qualify_tool_id

logger = logging.getLogger(__name__)


def _model_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return JSON schema for a pydantic model."""
    schema_fn = getattr(model, "model_json_schema", None)
    if callable(schema_fn):
        return schema_fn()
    return model.schema()


class InvokeToolInput(BaseModel):
    """Input schema for invoke_tool meta tool."""

    tool_name: str = Field(
        ...,
        description="目标工具名，必须精确匹配 tools_search 返回的工具名",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="目标工具的参数，根据 tools_search 返回的 input_schema 构造",
    )


class InvokeToolTool(Tool):
    """Invoke a deferred tool discovered via tools_search."""

    TOOL_NAME = "invoke_tool"
    TOOL_ID = "InvokeToolTool"

    def __init__(
        self,
        invoke_target_tool: Callable[
            [Any, InvokeToolInput],
            Awaitable[dict[str, Any]],
        ],
        *,
        language: str = "cn",
        agent_card_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        _ = language
        scope_id = str(agent_card_id or agent_id or "").strip()
        card = ToolCard(
            id=self.TOOL_ID,
            name=self.TOOL_NAME,
            description=(
                "调用按需可见工具。入参 tool_name 须与 tools_search / 导航列表中的注册名一致，"
                "arguments 根据 tools_search 返回的 input_schema 构造。"
                "目标工具不在当前 tools schema 中时，须通过本工具间接执行。"
            ),
            input_params=_model_schema(InvokeToolInput),
            properties={"resilience": {"timeout_s": None}},
        )
        if scope_id:
            card.id = qualify_tool_id(card, scope_id)
        super().__init__(card)
        self._invoke_target_tool = invoke_target_tool

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        logger.info("[InvokeTool] invoke inputs: %s", inputs)
        session = kwargs.get("session")
        kwargs_without_session = {
            k: v for k, v in kwargs.items() if k != "session"
        }
        try:
            parsed = InvokeToolInput(**(inputs or {}))
            return await self._invoke_target_tool(
                session, parsed, **kwargs_without_session
            )
        except Exception as exc:
            logger.warning("[InvokeTool] invoke failed: %s", exc, exc_info=True)
            return {
                "success": False,
                "error": str(exc),
                "tool_name": inputs.get("tool_name", ""),
            }

    async def stream(self, inputs: dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        if False:
            yield None
