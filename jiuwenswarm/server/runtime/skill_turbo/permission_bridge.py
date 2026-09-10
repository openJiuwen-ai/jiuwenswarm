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

import copy
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

# SkillTurbo 自有 session state key -- 与 openjiuwen 自身命名空间区分，所以前后用双下划线。
SKILL_TURBO_RESUME_CTX_KEY = "__skill_turbo_resume_ctx__"

# SkillTurbo 专用 agent_id 后缀：executor 和 resume 读取时用 '{card.id}__skill_turbo'，
# 使 checkpointer key 与 DeepAgent 隔离，避免 DeepAgent 的 post_run 覆盖
# executor 写入的 resume_ctx / node_artifacts。
SKILL_TURBO_ID_SUFFIX = "__skill_turbo"

logger = logging.getLogger(__name__)


def set_skill_turbo_id(session: Any, card: Any) -> None:
    """将 session 的 agent_id 设为 '{card.id}__skill_turbo'，使 SkillTurbo 的 checkpointer
    key 与 DeepAgent 隔离，避免 post_run 互相覆盖。

    必须在 session.pre_run() 之前调用。
    对 FakeSession 等无 _inner 的 stub 是 no-op。
    """
    if session is None or card is None:
        return
    card_id = getattr(card, "id", None)
    if not card_id:
        return
    inner = getattr(session, "_inner", None)
    if inner is None:
        return
    try:
        config = inner.config()
        skill_turbo_id = f"{card_id}{SKILL_TURBO_ID_SUFFIX}"
        config.set_agent_config(type("SkillTurboAgentConfig", (), {"id": skill_turbo_id})())
        logger.debug("[SkillTurboResume] set_skill_turbo_id: %s", skill_turbo_id)
    except Exception as exc:
        logger.warning("[SkillTurboResume] set_skill_turbo_id failed: %s", exc)


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

    调用方应已 pre_run（executor 在 _persist_node_artifacts 中 pre_run 过），
    本函数负责 update_state + post_run 完成落盘（与 node_artifacts 共一次 post_run）。

    ``task_states``：中断时的二层任务全量快照（含稳定 ``task_id``）。
    resume 时复用同一套 id，避免前端 taskRuns 因新 UUID 叠出两套 Stage。
    """
    if session is None:
        logger.warning("[SkillTurboResume] save_resume_ctx: session is None, skipping")
        return
    sid = _get_sid(session)
    entry: dict[str, Any] = {
        "plan_code": plan_code,
        "inputs": dict(inputs),
        "pending_tool_call_id": pending_tool_call_id,
        # update_state 使用 merge 语义；上一轮 mark_resume_in_flight 写入的
        # resume_in_flight=True 不会因新 entry 缺少该键而被移除。显式置 None
        # 让 merge 将其从持久化状态中删除，避免下一轮恢复被误判为重复答案。
        "resume_in_flight": None,
    }
    if task_states:
        entry["task_states"] = copy.deepcopy(task_states)
    # 与 save_node_artifacts 一致：pre_run 后 update_state 才能经 checkpointer 持久化。
    # 调用方若已在 pre_run 上下文里，重复 pre_run 是 no-op。
    try:
        await session.pre_run(inputs=None)
    except Exception as e:
        logger.warning(
            "[SkillTurboResume] save_resume_ctx pre_run failed: sid=%s err=%s", sid, e
        )
    try:
        session.update_state({SKILL_TURBO_RESUME_CTX_KEY: entry})
    except Exception as e:
        logger.warning(
            "[SkillTurboResume] save_resume_ctx update_state failed: sid=%s err=%s", sid, e
        )
        raise
    logger.info(
        "[SkillTurboResume] save_resume_ctx: sid=%s tcid=%s plan_code_len=%d task_states=%d",
        sid,
        pending_tool_call_id,
        len(plan_code or ""),
        len(entry.get("task_states") or []),
    )
    try:
        await session.post_run()
        logger.info("[SkillTurboResume] save_resume_ctx: persisted OK sid=%s", sid)
    except Exception as e:
        logger.warning("[SkillTurboResume] save_resume_ctx post_run failed: sid=%s err=%s", sid, e)


async def load_resume_ctx(session: Any) -> dict[str, Any] | None:
    """从 checkpointer 读取断点上下文。返回 None 表示无可恢复的 SkillTurbo 中断。"""
    if session is None:
        logger.warning("[SkillTurboResume] load_resume_ctx: session is None")
        return None
    sid = _get_sid(session)
    try:
        await session.pre_run(inputs=None)
    except Exception as e:
        logger.warning(
            "[SkillTurboResume] load_resume_ctx pre_run failed: sid=%s err=%s", sid, e
        )
        return None
    try:
        state = session.get_state(SKILL_TURBO_RESUME_CTX_KEY)
    except Exception as e:
        logger.warning(
            "[SkillTurboResume] load_resume_ctx get_state failed: sid=%s err=%s", sid, e
        )
        return None
    if isinstance(state, dict) and state.get("plan_code"):
        logger.info(
            "[SkillTurboResume] load_resume_ctx: found ctx sid=%s tcid=%s",
            sid, state.get("pending_tool_call_id"),
        )
        return copy.deepcopy(state)
    logger.info("[SkillTurboResume] load_resume_ctx: no ctx found sid=%s", sid)
    return None


async def clear_resume_ctx(session: Any) -> None:
    """清除断点上下文（resume 跑通后调用）。"""
    if session is None:
        return
    sid = _get_sid(session)
    try:
        session.update_state({SKILL_TURBO_RESUME_CTX_KEY: None})
    except Exception:
        logger.debug(
            "[SkillTurboResume] clear_resume_ctx update_state failed", exc_info=True
        )
    logger.info("[SkillTurboResume] clear_resume_ctx: cleared sid=%s", sid)


async def mark_resume_in_flight(session: Any, resume_ctx: dict[str, Any]) -> None:
    """Mark resume_ctx as in-flight so duplicate answer submits are ignored.

    Same-process double-click / retry can otherwise load the same pending tcid
    and start a second resume stream (gateway does not cancel interrupt-resume).

    Persists via ``update_state`` + ``post_run`` so a later request that
    ``load_resume_ctx`` (pre_run from checkpointer) also sees the flag.
    """
    if session is None or not isinstance(resume_ctx, dict):
        return
    sid = _get_sid(session)
    marked = dict(resume_ctx)
    marked["resume_in_flight"] = True
    try:
        await session.pre_run(inputs=None)
    except Exception as e:
        logger.warning(
            "[SkillTurboResume] mark_resume_in_flight pre_run failed: sid=%s err=%s",
            sid,
            e,
        )
    try:
        session.update_state({SKILL_TURBO_RESUME_CTX_KEY: marked})
    except Exception:
        logger.debug(
            "[SkillTurboResume] mark_resume_in_flight update_state failed",
            exc_info=True,
        )
        return
    try:
        await session.post_run()
    except Exception as e:
        logger.warning(
            "[SkillTurboResume] mark_resume_in_flight post_run failed: sid=%s err=%s",
            sid,
            e,
        )


async def clear_resume_in_flight(session: Any) -> None:
    """Clear the in-flight flag if resume_ctx is still present."""
    if session is None:
        return
    try:
        state = session.get_state(SKILL_TURBO_RESUME_CTX_KEY)
    except Exception as e:
        logger.warning(
            "[SkillTurboResume] clear_resume_in_flight get_state failed: err=%s",
            e,
        )
        return
    if not isinstance(state, dict) or not state.get("resume_in_flight"):
        return
    cleaned = dict(state)
    cleaned.pop("resume_in_flight", None)
    try:
        session.update_state({SKILL_TURBO_RESUME_CTX_KEY: cleaned})
    except Exception:
        logger.debug(
            "[SkillTurboResume] clear_resume_in_flight update_state failed",
            exc_info=True,
        )
