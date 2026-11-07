# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Optional SubagentRail-backed text delegation for Designer nodes.

``SubagentRail`` is a DeepAgent ``before_model_call`` rail that registers
task / session tools. It is not a graph-node scheduler. Designer nodes keep
dedicated handlers; when ``config.delegate == "subagent"`` a registered
runner can take over text generation. Chat agents that already have
SubagentRail can register that runner, or call ``designer.graph.patch``
to rewrite the graph.
"""

from __future__ import annotations

from typing import Awaitable, Callable

DesignerSubagentRunner = Callable[[str], Awaitable[str]]

_runner: DesignerSubagentRunner | None = None


def register_designer_subagent_runner(runner: DesignerSubagentRunner | None) -> None:
    """Install or clear the optional SubagentRail-backed text runner."""
    global _runner
    _runner = runner


def get_designer_subagent_runner() -> DesignerSubagentRunner | None:
    return _runner


async def try_designer_subagent(prompt: str) -> str:
    runner = _runner
    if runner is None:
        return ""
    text = await runner(prompt)
    return str(text or "").strip()


async def complete_designer_node_text(
    prompt: str,
    *,
    delegate: str = "handler",
    max_tokens: int = 8192,
) -> str:
    """Run text via a registered subagent, otherwise the default chat model."""
    from jiuwenswarm.server.runtime.designer.handlers.common import complete_designer_text

    if str(delegate or "").strip() == "subagent":
        text = await try_designer_subagent(prompt)
        if text:
            return text
    return await complete_designer_text(prompt, max_tokens=max_tokens)
