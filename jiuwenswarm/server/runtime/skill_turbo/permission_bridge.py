# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo ↔ PermissionInterruptRail 胶水层（纯函数，无业务状态）。

职责：
1. 构造 ``AgentCallbackContext``（含 resume 用户输入），交给护栏链 ``before_tool_call``。
2. 从 ``AbortError`` 中抽出 ``ToolInterruptException``（含 ``InterruptRequest`` 与 ``ToolCall``）。
3. 把 SkillTurbo 的「断点上下文」存到 openjiuwen ``Session`` state（键 ``__skill_turbo_resume_ctx__``），
   走 checkpointer 持久化，保证多 worker/多实例部署的 HITL 恢复可靠。

设计原则参见 SkillTurbo 安全护栏新方案 v2 §4。
"""


from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)

# AbortError 经 plan_node 统一 re-export，不在本模块直连 openjiuwen（见 plan_node 注释）。
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

# ── 单一权威定义 re-export ──
# SKILL_TURBO_RESUME_CTX_KEY / SKILL_TURBO_ID_SUFFIX / set_skill_turbo_id 的
# 权威定义在 resume_context.py（checkpointer 键同一性是 HITL resume 链路硬约束，
# 禁止双份字面量漂移）。此处 re-export 仅为兼容既有 import 方（executor /
# interface_deep / skill_turbo_tools / 测试），调用方全部无需改动。
from jiuwenswarm.server.runtime.skill_turbo.resume_context import (  # noqa: F401
    SKILL_TURBO_ID_SUFFIX,
    SKILL_TURBO_RESUME_CTX_KEY,
    set_skill_turbo_id,
)

logger = logging.getLogger(__name__)


@dataclass
class SkillTurboToolCall:
    """SkillTurbo 内部用的轻量 tool_call 对象。

    不依赖 openjiuwen pydantic ``ToolCall``（它强制要求 ``arguments: str``、
    需要 ``type`` 字段），rail 只通过 ``id/name/arguments`` 属性访问，
    用 dataclass 满足鸭子类型即可。
    """

    id: str
    name: str
    arguments: dict[str, Any]


def build_tool_ctx(
    *,
    session: Any,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: str,
    resume_user_input: Any | None = None,
) -> AgentCallbackContext:
    """构造一个用于 ``before_tool_call`` / ``after_tool_call`` 的 ctx。

    Args:
        session: 当前 openjiuwen Session 实例（可为 None，rail 内部会兼容）。
        tool_name: 工具名称。
        tool_args: 工具参数（kwargs 透传）。
        tool_call_id: 本次 tool 调用 id；resume 时必须与中断时一致，
            才能被 ``BaseInterruptRail._get_user_input`` 命中。
        resume_user_input: 用户对该 tool_call_id 的回复。``None`` 表示首次调用。
            可以是 ``ConfirmPayload`` 实例 / dict / 任意 rail 接受的载荷。
    """
    tool_call = SkillTurboToolCall(id=tool_call_id, name=tool_name, arguments=tool_args)
    extra: dict[str, Any] = {}
    if resume_user_input is not None:
        # rail 通过 ctx.extra[RESUME_USER_INPUT_KEY] 取用户回复
        extra[RESUME_USER_INPUT_KEY] = resume_user_input
    return AgentCallbackContext(
        agent=None,
        session=session,
        inputs=ToolCallInputs(
            tool_name=tool_name,
            tool_call=tool_call,
            tool_args=tool_args,
        ),
        context=None,
        extra=extra,
    )


def extract_tool_interrupt(exc: BaseException) -> ToolInterruptException | None:
    """沿 ``__cause__`` / ``cause`` 链抽出 ``ToolInterruptException``。

    PermissionInterruptRail 抛出的链路是：
        ``AbortError(reason=..., cause=ToolInterruptException(request, tool_call))``

    其中 ``cause`` 既被设置到 ``AbortError.cause`` 字段，也通过 ``raise ... from cause``
    挂到 ``__cause__``。两者择一即可。
    """
    visited: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in visited:
        if isinstance(cur, ToolInterruptException):
            return cur
        visited.add(id(cur))
        nxt = getattr(cur, "__cause__", None)
        if nxt is None:
            nxt = getattr(cur, "cause", None)
        cur = nxt
    return None


def is_blocking_abort(exc: BaseException) -> bool:
    """判断异常是否承载工具中断（HITL）语义。"""
    if not isinstance(exc, AbortError):
        return False
    return extract_tool_interrupt(exc) is not None


def build_interaction_output_from_abort(
    exc: BaseException,
) -> Any | None:
    """从 AbortError 构造 ``OutputSchema(type="__interaction__")``。

    从 ``AbortError`` 的 cause 链抽出 ``ToolInterruptException``，再用
    ``ToolCallInterruptRequest.from_tool_call`` 包装，最终构造
    ``InteractionOutput(id=tcid, value=tool_call_request)`` 并放入
    ``OutputSchema(type="__interaction__", payload=interaction_payload)``。

    供 SkillTurbo executor（中断侧主动写流）和 DeepAdapter（``_emit_skill_turbo_hitl_chunks``
    二次中断兜底）共用，保持构造逻辑一致。

    Args:
        exc: ``AbortError``（其 cause 链含 ``ToolInterruptException``）。

    Returns:
        ``OutputSchema`` 实例，或 ``None``（无 tic / 构造失败）。
    """
    from openjiuwen.core.session.interaction.interaction import InteractionOutput
    from openjiuwen.core.session.stream import OutputSchema
    from openjiuwen.core.single_agent.interrupt.response import (
        ToolCallInterruptRequest,
    )

    tic = extract_tool_interrupt(exc)
    if tic is None:
        logger.warning(
            "[SkillTurboBridge] build_interaction_output_from_abort: no ToolInterruptException in cause chain"
        )
        return None

    tc_id = tic.tool_call.id if tic.tool_call else ""

    try:
        tool_call_request = ToolCallInterruptRequest.from_tool_call(
            tic.request, tic.tool_call,
        )
    except Exception as tcr_exc:
        tool_call_request = tic.request
        logger.warning(
            "[SkillTurboBridge] from_tool_call failed: %s; falling back to raw tic.request",
            tcr_exc,
            exc_info=True,
        )

    interaction_payload = InteractionOutput(
        id=tc_id or "",
        value=tool_call_request,
    )
    return OutputSchema(
        type="__interaction__",
        index=0,
        payload=interaction_payload,
    )


def _get_sid(session: Any) -> str:
    """获取 session ID，兼容 session_id 属性和 get_session_id() 方法。"""
    sid = getattr(session, "session_id", None)
    if sid is None:
        getter = getattr(session, "get_session_id", None)
        if callable(getter):
            try:
                sid = getter()
            except Exception:
                sid = "?"
                logger.debug("[SkillTurboResume] get_session_id failed", exc_info=True)
        else:
            sid = "?"
    return str(sid) if sid else "?"


# ──────────────────────── Resume Context ────────────────────────
# resume_ctx 走 session state + checkpointer 持久化，保证多 worker/多实例
# 部署的 HITL 恢复可靠（同一 session_id 在任何进程都能从 checkpointer 读到）。
#
# save: session.update_state() + post_run 落盘。
# load: session.pre_run() + get_state() 从 checkpointer 恢复。
# clear: session.update_state(key=None)。
#
# 与 node_artifacts 共享同一持久化语义，避免「产物在、断点不在」的半恢复状态。


async def save_resume_ctx(
    session: Any,
    *,
    plan_code: str,
    inputs: dict[str, Any],
    pending_tool_call_id: str,
    task_states: list[dict[str, Any]] | None = None,
) -> None:
    """中断时保存断点上下文到 session state（checkpointer 持久化）。

    薄委托至 ResumeContextManager.save（单一 owner）。保留签名供调用方逐步迁移。
    """
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager

    mgr = ResumeContextManager(session)
    await mgr.save(
        plan_code=plan_code,
        inputs=inputs,
        pending_tool_call_id=pending_tool_call_id,
        task_states=task_states,
    )


async def load_resume_ctx(session: Any) -> dict[str, Any] | None:
    """从 checkpointer 读取断点上下文。返回 None 表示无可恢复的 SkillTurbo 中断。

    薄委托至 ResumeContextManager.load（语义逐位一致）。
    """
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager

    mgr = ResumeContextManager(session)
    return await mgr.load()


async def clear_resume_ctx(session: Any) -> None:
    """清除断点上下文（resume 跑通后调用）。

    薄委托至 ResumeContextManager.clear。注意：manager.clear() 使用 isolated
    通道强制落盘，而非原 session 的 update_state(None)——修复键空间错误。
    若调用方已自行 pre_run，manager 会在 isolated session 上独立 pre_run。
    """
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager

    mgr = ResumeContextManager(session)
    await mgr.clear()


async def mark_resume_in_flight(session: Any, resume_ctx: dict[str, Any]) -> None:
    """Mark resume_ctx as in-flight so duplicate answer submits are ignored.

    薄委托至 ResumeContextManager.mark_in_flight。
    """
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager

    mgr = ResumeContextManager(session)
    await mgr.mark_in_flight(resume_ctx)


async def clear_resume_in_flight(session: Any) -> None:
    """Clear the in-flight flag if resume_ctx is still present.

    薄委托至 ResumeContextManager.clear_in_flight（isolated 通道强制落盘）。
    """
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager

    mgr = ResumeContextManager(session)
    await mgr.clear_in_flight()
