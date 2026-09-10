# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cancel orphaned active todos before a fresh (non-resume) user turn."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent import create_agent_session

from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    is_resume_user_query,
    mark_skip_invoke_task_update_sync,
    clear_skip_invoke_task_update_sync,
    clear_stale_todo_ids,
    set_stale_todo_ids
)
from jiuwenswarm.server.runtime.agent_adapter.interrupt_resume_helpers import (
    load_session_todo_items,
    set_todo_resume_snapshot_pending,
)
from jiuwenswarm.server.runtime.agent_adapter.plan_pause_helpers import (
    cancel_pending_todos_on_tool,
    post_agent_execute_for_session,
)

logger = logging.getLogger(__name__)


def should_cancel_stale_active_todos(request: Any, params: dict[str, Any]) -> bool:
    """Return True when this turn should isolate prior active todos on disk.

    Skips heartbeat, non-plan modes, supplement turns, structured replies, and
    explicit resume phrases (继续/接着做). Aligns with skill_prompt: a new user
    message replaces the prior task unless the user explicitly continues.
    """
    session_id = str(getattr(request, "session_id", "") or "")
    if session_id.startswith("heartbeat"):
        return False

    # dev-stable 实际请求 params.mode 为 "agent"（统一智能体模式），
    # 历史遗留 "agent.plan"（plan 子模式）。两者都带 todo 任务循环，
    # 都应参与旧 todo 清理；其余 mode（chat/team/heartbeat 等）才跳过。
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


def _serialize_todos_for_log(todos: list[Any]) -> str:
    payload: list[dict[str, Any]] = []
    for todo in todos:
        status = todo.status
        status_value = status.value if hasattr(status, "value") else str(status)
        payload.append(
            {
                "id": getattr(todo, "id", ""),
                "content": getattr(todo, "content", ""),
                "activeForm": getattr(todo, "activeForm", ""),
                "status": status_value,
            }
        )
    return json.dumps(payload, ensure_ascii=False) if payload else "[]"


async def prepare_stale_todo_cleanup_for_request(
    request: Any,
    *,
    agent_card: Any,
    get_todo_modify_tool: Callable[[str], Any],
    runtime_session: Any = None,
) -> bool:
    """Cancel active todos before a fresh user turn; log todo.json and set skip flag.

    ``runtime_session`` is the live ``_interaction_session`` that
    ``TaskExecutionRail`` reads in ``before_invoke``. The skip flag must be
    set on it (in-memory), not on the throwaway session used for the disk-only
    cancel — those are different objects, and a flag on the throwaway is
    invisible to ``before_invoke``, so the LLM's later ``todo_modify`` would
    re-broadcast the whole stale todo snapshot.
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

    session = create_agent_session(session_id=session_id, card=agent_card)
    await session.pre_run(inputs=None)
    try:
        # Drop skip flag left when a prior turn crashed after cleanup but before
        # TaskExecutionRail.after_invoke could clear it (e.g. resume/supplement).
        clear_skip_invoke_task_update_sync(session)
        if runtime_session is not None and runtime_session is not session:
            clear_skip_invoke_task_update_sync(runtime_session)
            clear_stale_todo_ids(runtime_session)

        should_cancel = should_cancel_stale_active_todos(request, params)
        if not should_cancel:
            logger.debug(
                "[JiuWenClaw] prepare_stale_todo_cleanup: should_cancel=False session_id=%s",
                session_id,
            )
            await post_agent_execute_for_session(session)
            return False

        modify_tool = get_todo_modify_tool(session_id)
        if modify_tool is None:
            logger.info(
                "[JiuWenClaw] prepare_stale_todo_cleanup: EARLY RETURN (modify_tool is None) session_id=%s",
                session_id,
            )
            await post_agent_execute_for_session(session)
            return False

        # 不用 is_interrupt_recovery_injected 一票否决：该哨兵只表示"残留了上次
        # 中断的产物摘要"，不等于"用户本条消息在续跑"。续跑已由
        # should_cancel_stale_active_todos 内的 is_resume_user_query 判定并在此前返回。

        todos = await load_session_todo_items(modify_tool, session_id)
        if not todos:
            logger.debug(
                "[JiuWenClaw] prepare_stale_todo_cleanup: no todos session_id=%s",
                session_id,
            )
            await post_agent_execute_for_session(session)
            return False

        logger.info(
            "[JiuWenClaw] 因旧任务已停止、新消息不是续跑，清理上一轮的残留 todo; "
            "session_id=%s request_id=%s todo.json=%s",
            session_id,
            getattr(request, "request_id", ""),
            _serialize_todos_for_log(todos),
        )

        # skip 标志必须落在运行时 session（before_invoke 读的 _interaction_session）上：
        # 临时 session 与运行时 session 是不同对象，标在临时 session 上 before_invoke
        # 看不见 → skip_invoke=False → 旧 todo 会在随后 todo_modify 时整张回灌前端。
        # 必须先设标志再 cancel：cancel 崩溃时（曾致两道防线一起失效）最坏只剩
        # 磁盘残留，skip_invoke 仍会捕获旧 todo id 过滤回灌，旧列表不再串台。
        skip_session = runtime_session if runtime_session is not None else session
        set_todo_resume_snapshot_pending(skip_session, pending=False)
        mark_skip_invoke_task_update_sync(skip_session)
        set_stale_todo_ids(skip_session, {t.id for t in todos})

        # cancel active todo（如有）；即使只剩 completed/cancelled 残留也要 mark skip，
        # 让广播层过滤旧 todo 防止跨请求串台。cancel 自身的失败只降级为磁盘残留。
        try:
            await cancel_pending_todos_on_tool(modify_tool, session_id)
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] cancel_pending_todos_on_tool failed "
                "session_id=%s: %s (skip flag already set; stale todos remain "
                "on disk but broadcasts stay filtered)",
                session_id,
                exc,
            )
        await post_agent_execute_for_session(session)
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
