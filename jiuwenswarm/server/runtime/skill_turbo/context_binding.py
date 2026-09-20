# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill_turbo 专用 ContextVar 的 bind/reset 函数对（单一收口）。

``stream_event_rail.before_tool_call`` 调用 ``bind_skill_turbo_context`` 统一绑定
7 个 ContextVar（adapter / request_metadata / workspace_dir / interactive_ask /
resume_answers / outer_todo_active / subagent_parent_session），tokens 存在 ctx 的
task-local 属性上；``after_tool_call`` / ``after_invoke`` / ``on_model_exception``
调用 ``reset_skill_turbo_context`` 一处循环复位——消除散落的逐个 reset 与异常
路径漏 reset（R5/R8 根源）。

后续新增通道（如 outer_todo_pending）在 ``bind`` 中加一段、在 ``_reset`` 对应
reset 函数中加一行即可，不再散绑。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_BIND_TOKENS_ATTR = "_jiuwenswarm_skill_turbo_bind_tokens"


def _bind_tokens(ctx: Any) -> dict:
    """task-local token 存储（随 ctx 生命周期，不进共享 extra dict）。"""
    tokens = getattr(ctx, _BIND_TOKENS_ATTR, None)
    if not isinstance(tokens, dict):
        tokens = {}
        setattr(ctx, _BIND_TOKENS_ATTR, tokens)
    return tokens


def bind_skill_turbo_context(
    ctx: Any,
    *,
    adapter: Any = None,
    request_metadata: Any = None,
    parent_session: Any = None,
    resume_answers: Any = None,
    outer_todo_active: Any = None,
) -> None:
    """before_tool_call 调用：统一绑定 7 个 skill_turbo ContextVar。

    各绑定保持原条件语义：值缺失（None / 非 bool）不绑、不产生 token。
    """
    tokens = _bind_tokens(ctx)

    if adapter is not None:
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            set_current_skill_turbo_adapter,
        )

        tokens["adapter"] = set_current_skill_turbo_adapter(adapter)

    if request_metadata is not None:
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            set_current_request_metadata,
        )

        tokens["metadata"] = set_current_request_metadata(request_metadata)

        # workspace / interactive_ask 与 metadata 同源、同条件（metadata 是 dict 才有）
        if isinstance(request_metadata, dict):
            from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
                set_effective_request_workspace_dir,
                set_interactive_ask,
            )

            epd = request_metadata.get("effective_project_dir")
            if isinstance(epd, str) and epd.strip():
                # 与 extract_effective_project_dir 同一语义：strip 后非空才绑定
                tokens["workspace"] = set_effective_request_workspace_dir(epd.strip())
            interactive_ask = request_metadata.get("interactive_ask")
            if interactive_ask is not None:
                tokens["interactive_ask"] = set_interactive_ask(bool(interactive_ask))

    if parent_session is not None:
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            set_subagent_parent_session,
        )

        # 解包 _parent：绑定 DeepAgent 主会话本体（与原实现一致）
        actual = getattr(parent_session, "_parent", parent_session)
        tokens["subagent_parent"] = set_subagent_parent_session(actual)

    if resume_answers is not None:
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            set_skill_turbo_resume_answers,
        )

        tokens["resume_answers"] = set_skill_turbo_resume_answers(resume_answers)

    if isinstance(outer_todo_active, bool):
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            set_skill_turbo_outer_todo_active,
        )

        tokens["outer_todo"] = set_skill_turbo_outer_todo_active(outer_todo_active)


def _reset_adapter(token: Any) -> None:
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        reset_current_skill_turbo_adapter,
    )

    reset_current_skill_turbo_adapter(token)


def _reset_metadata(token: Any) -> None:
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        reset_current_request_metadata,
    )

    reset_current_request_metadata(token)


def _reset_workspace(token: Any) -> None:
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        reset_effective_request_workspace_dir,
    )

    reset_effective_request_workspace_dir(token)


def _reset_interactive_ask(token: Any) -> None:
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        reset_interactive_ask,
    )

    reset_interactive_ask(token)


def _reset_resume_answers(token: Any) -> None:
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        reset_skill_turbo_resume_answers,
    )

    reset_skill_turbo_resume_answers(token)


def _reset_outer_todo(token: Any) -> None:
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        reset_skill_turbo_outer_todo_active,
    )

    reset_skill_turbo_outer_todo_active(token)


def _reset_subagent_parent(token: Any) -> None:
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        reset_subagent_parent_session,
    )

    reset_subagent_parent_session(token)


_RESETTERS = {
    "adapter": _reset_adapter,
    "metadata": _reset_metadata,
    "workspace": _reset_workspace,
    "interactive_ask": _reset_interactive_ask,
    "resume_answers": _reset_resume_answers,
    "outer_todo": _reset_outer_todo,
    "subagent_parent": _reset_subagent_parent,
}


def reset_skill_turbo_context(ctx: Any) -> None:
    """after_tool_call / after_invoke / on_model_exception 调用：一处循环复位。

    幂等：已 reset 的 token pop 得 None 跳过。凡 bind 存入的 token 必被复位，
    不依赖 ctx.extra 形态（消除漏 reset）。单个 reset 失败不影响其余 token。
    """
    tokens = getattr(ctx, _BIND_TOKENS_ATTR, None)
    if not isinstance(tokens, dict):
        return
    for name in list(tokens.keys()):
        token = tokens.pop(name, None)
        if token is None:
            continue
        resetter = _RESETTERS.get(name)
        if resetter is None:
            continue
        try:
            resetter(token)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug(
                "[context_binding] reset %s token failed", name, exc_info=True
            )


__all__ = ["bind_skill_turbo_context", "reset_skill_turbo_context"]
