# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The agent-side face of a tool a connected client runs in the background.

Calling a ``DetachedClientTool`` does not wait for the client: it records the
call with the Session's ``ClientToolAdapter``, which asks the client to run it,
and at once returns that the call was accepted. The turn goes on (or ends) and
the result arrives later as a separate input. See
``jiuwenswarm.runtime.client_tool_adapter``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure
from openjiuwen.core.foundation.tool.schema import ToolOutput

from jiuwenswarm.runtime.client_tool_adapter import ClientToolAdapter, ClientToolError, ClientToolSpec

logger = logging.getLogger(__name__)

_DESCRIPTION_SUFFIX = (
    " Runs on the user's client in the background: calling it only starts the call, "
    "and its result arrives later as a separate message. Do not wait for it, poll it or call it again."
)


def _tool_call_id(kwargs: Mapping[str, Any]) -> str:
    ctx = kwargs.get("_tool_callback_context")
    tool_call = getattr(getattr(ctx, "inputs", None), "tool_call", None)
    call_id = tool_call.get("id") if isinstance(tool_call, Mapping) else getattr(tool_call, "id", None)
    return str(call_id or "").strip() or f"client-call-{uuid.uuid4().hex}"


class DetachedClientTool(Tool):
    """A client tool whose calls run in the background on the Session's client."""

    # AbilityManager then passes the rail callback context, which carries the tool call id.
    accepts_tool_callback_context = True

    def __init__(
        self,
        spec: ClientToolSpec,
        *,
        session_id: str,
        adapter: Callable[[], ClientToolAdapter | None],
    ) -> None:
        card = ToolCard(
            name=spec.tool_name,
            description=(spec.description or f"Client tool {spec.name}.") + _DESCRIPTION_SUFFIX,
            input_params=dict(spec.input_schema),
        )
        # The client declared the tool to be used now. With progressive tool discovery an
        # undeclared card is deferred behind tool_search, which the model may never call.
        card.exposure = ToolExposure.DIRECT
        card.set_exposure_declared(True)
        super().__init__(card)
        self.spec = spec
        self.session_id = session_id
        self._adapter = adapter

    async def invoke(self, inputs: Any, **kwargs: Any) -> ToolOutput:
        adapter = self._adapter()
        if adapter is None:
            return ToolOutput(success=False, error="Client tools are not available right now.")
        if isinstance(inputs, str):
            try:
                inputs = json.loads(inputs) if inputs.strip() else {}
            except ValueError:
                return ToolOutput(success=False, error=f"Client tool '{self.card.name}' arguments must be JSON.")
        if not isinstance(inputs, Mapping):
            return ToolOutput(success=False, error=f"Client tool '{self.card.name}' arguments must be an object.")
        try:
            call = await adapter.open_call(self.session_id, _tool_call_id(kwargs), self.card.name, inputs)
        except ClientToolError as exc:
            return ToolOutput(success=False, error=str(exc), data={"code": exc.code})
        return ToolOutput(success=True, data={
            "content": (
                f"Client tool {self.card.name} started on the user's client (call_id={call.call_id}). "
                "Its result will arrive later as a separate message. Do not call it again; continue "
                "with other work or reply to the user, and do not guess its result."
            ),
            "call_id": call.call_id,
            "status": call.status.value,
        })

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


__all__ = ["DetachedClientTool"]
