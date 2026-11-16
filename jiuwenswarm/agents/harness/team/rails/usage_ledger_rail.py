# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Usage-ledger rail: charge every model call an agent makes to its run.

A token budget only holds if every call is counted, and the count has to come
from the call itself. This rail reads the usage the model client attached to the
response (``ctx.inputs.response.usage_metadata``) after each model call and hands
one row to ``on_usage`` -- typically ``WorkflowRunLog.record_usage`` plus a line
in ``usage.jsonl``. A response that carries no usage is not skipped: it is
recorded with ``measured=False``, because a call the ledger cannot see is how a
budget silently stops meaning anything.
"""

from __future__ import annotations

from typing import Callable

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail


class UsageLedgerRail(DeepAgentRail):
    """Report each model call's token usage to a callback, with the stage it belongs to."""

    def __init__(self, on_usage: Callable[[dict], None], *, stage: str, step: str = "") -> None:
        super().__init__()
        self._on_usage = on_usage
        self.stage = stage
        self.step = step
        self.calls = 0

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        response = getattr(getattr(ctx, "inputs", None), "response", None)
        usage = getattr(response, "usage_metadata", None)
        self.calls += 1
        row = {
            "stage": self.stage,
            "step": self.step,
            "call": self.calls,
            "model": getattr(usage, "model_name", "") or "",
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "measured": usage is not None and bool(getattr(usage, "total_tokens", 0)),
        }
        self._on_usage(row)


__all__ = ["UsageLedgerRail"]
