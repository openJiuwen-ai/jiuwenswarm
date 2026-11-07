# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read the model's text out of an AFTER_MODEL_CALL context.

Shared by the rails that inspect what an agent just wrote. It exists as its own
module because getting this wrong is silent: the response lives on
``ctx.inputs.response`` -- ``inputs`` is the ``ModelCallInputs`` for the event --
and a rail reading ``ctx.response`` instead receives ``None``, scans an empty
string, finds nothing, and is indistinguishable at the log from a rail that
looked at a clean draft.
"""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext


def response_text(ctx: AgentCallbackContext | None) -> str:
    """Return the assistant text for this call, or "" when there is none.

    ``ctx.response`` is still consulted as a fallback so the helper keeps
    working if a host ever populates it.
    """
    if ctx is None:
        return ""
    response = getattr(getattr(ctx, "inputs", None), "response", None)
    if response is None:
        response = getattr(ctx, "response", None)
    if response is None:
        return ""
    if isinstance(response, str):
        return response

    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Multimodal content blocks: keep the text parts, drop images and the rest.
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        return "".join(p for p in parts if p)

    text = getattr(response, "text", None)
    return str(text or "") if text is not None else ""


__all__ = ["response_text"]
