# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Isolate prior-generation todos before a fresh (non-resume) user turn.

请求隔离（P1-2）：新一轮非续跑用户消息开始时递增 todo generation_token
（经 flag proxy 同时落临时 session 与运行时 session）。旧代 todo 条目由
广播层（todo.updated / task.update）和 todo 工具按 token 一致性过滤，
取代旧的「磁盘 cancel + skip 标志 + stale/pre/current ids 快照」多层体系。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent import create_agent_session

from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    bump_todo_generation_token,
    is_resume_user_query,
)
from jiuwenswarm.server.runtime.agent_adapter.interrupt_resume_helpers import (
    set_todo_resume_snapshot_pending,
)
from jiuwenswarm.server.runtime.agent_adapter.plan_pause_helpers import (
    post_agent_execute_for_session,
)
from jiuwenswarm.server.runtime.agent_adapter.session_flag_proxy import build_flag_proxy

logger = logging.getLogger(__name__)


def should_cancel_stale_active_todos(request: Any, params: dict[str, Any]) -> bool:
    """Return True when this turn starts a new task generation.

    Skips heartbeat, non-plan modes, supplement turns, structured replies, and
    explicit resume phrases (继续/接着做). Aligns with skill_prompt: a new user
    message replaces the prior task unless the user explicitly continues.
    """
    session_id = str(getattr(request, "session_id", "") or "")
    if session_id.startswith("heartbeat"):
        return False

    # dev-stable 实际请求 params.mode 为 "agent"（统一智能体模式），
    # 历史遗留 "agent.plan"（plan 子模式）。两者都带 todo 任务循环，
    # 都应切换新代；其余 mode（chat/team/heartbeat 等）才跳过。
    mode = str(params.get("mode", "agent.plan") or "agent.plan").strip()
    if mode not in ("agent", "agent.plan"):
        return False

    if params.get("is_supplement"):
        return False

    query = params.get("query")
    if isinstance(query, InteractiveInput):
        return False

    if params.get("answers"):
        return False

    if is_resume_user_query(str(query or "")):
        return False

    return True


async def prepare_stale_todo_cleanup_for_request(
    request: Any,
    *,
    agent_card: Any,
    runtime_session: Any = None,
) -> bool:
    """Bump the todo generation token before a fresh user turn.

    旧任务已停止、新消息不是续跑 → 递增 generation_token：旧代条目在
    todo.updated / task.update 广播与 todo_list（LLM 视图）中按 token 过滤，
    不再回灌前端。resume / supplement / 权限应答等续跑轮不 bump，旧代
    todo 保持可见。

    ``runtime_session`` 是 ``TaskExecutionRail`` / ``StreamEventRail`` 在
    before_invoke 实际读取的运行时 session（``_interaction_session``）。
    token 经 proxy 同时落运行时 session（本轮广播层可见）与临时 session
    （post_agent_execute 落 checkpointer，进程重启后兜底）。
    """
    session_id = str(getattr(request, "session_id", "") or "").strip()
    if not session_id or agent_card is None:
        logger.info(
            "[JiuWenClaw] prepare_stale_todo_cleanup: EARLY RETURN session_id=%s "
            "agent_card=%s runtime_session=%s",
            session_id,
            "None" if agent_card is None else "bound",
            "None" if runtime_session is None else "bound",
        )
        return False

    params = request.params if isinstance(getattr(request, "params", None), dict) else None
    if params is None:
        logger.info(
            "[JiuWenClaw] prepare_stale_todo_cleanup: EARLY RETURN (no params) session_id=%s",
            session_id,
        )
        return False

    if not should_cancel_stale_active_todos(request, params):
        logger.debug(
            "[JiuWenClaw] prepare_stale_todo_cleanup: keep generation session_id=%s",
            session_id,
        )
        return False

    session = create_agent_session(session_id=session_id, card=agent_card)
    await session.pre_run(inputs=None)
    try:
        flag_proxy = build_flag_proxy(session, runtime_session)
        # 旧任务被新消息取代：清 resume 快照待处理标志 + 递增 generation
        # token（先 bump 再落盘；bump 后即使后续步骤崩溃，旧代条目也已被
        # 隔离，最坏退化为磁盘残留）。
        set_todo_resume_snapshot_pending(flag_proxy, pending=False)
        token = bump_todo_generation_token(flag_proxy)
        await post_agent_execute_for_session(session)

        logger.info(
            "[JiuWenClaw] 因旧任务已停止、新消息不是续跑，切换新 todo 代际; "
            "session_id=%s request_id=%s generation_token=%s",
            session_id,
            getattr(request, "request_id", ""),
            token,
        )
        return True
    except Exception as exc:
        logger.warning(
            "[JiuWenClawDeepAdapter] prepare_stale_todo_cleanup_for_request failed "
            "session_id=%s: %s",
            session_id,
            exc,
        )
        return False
    finally:
        await session.post_run()
