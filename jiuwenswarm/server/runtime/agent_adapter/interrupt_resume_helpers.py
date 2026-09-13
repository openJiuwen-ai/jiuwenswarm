# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for connection-interrupt resume (distinct from plan-mode cancel pause)."""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent import create_agent_session
from openjiuwen.harness.tools.todo import TodoModifyTool, TodoItem

from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    TODO_RESUME_SNAPSHOT_PENDING_KEY,
    build_interrupt_resume_decision_prompt,
    format_todo_snapshot_lines,
    has_active_todo_items,
    is_resume_user_query,
)

from jiuwenswarm.server.runtime.agent_adapter.plan_pause_helpers import (
    is_interrupt_recovery_injected,
    mark_interrupt_recovery_injected,
    merge_supplementary_into_request_params,
    post_agent_execute_for_session,
    read_plan_pause_from_session,
)

logger = logging.getLogger(__name__)


def set_todo_resume_snapshot_pending(session: Any, *, pending: bool) -> None:
    session.update_state({TODO_RESUME_SNAPSHOT_PENDING_KEY: bool(pending)})


async def load_session_todo_items(
    modify_tool: TodoModifyTool,
    session_id: str,
) -> list[TodoItem]:
    try:
        todos = await modify_tool.load_todos(session_id)
        return todos if isinstance(todos, list) else []
    except Exception as exc:
        logger.debug(
            "[interrupt_resume] load todos failed session_id=%s: %s",
            session_id,
            exc,
        )
        return []


async def prepare_interrupt_resume_for_request(
    adapter: Any,
    request: Any,
    *,
    runtime_session: Any = None,
) -> None:
    """Before agent.plan run: inject resume guidance when user continues an interrupted task.

    ``runtime_session`` 是 before_invoke 实际读取的运行时 session（_interaction_session）。
    哨兵与 snapshot-pending 标志必须落在它上面，否则 before_invoke 看不见（临时 session 是
    不同对象，标志标在那等价于没标）。临时 session 仍用于走 checkpointer 落盘的磁盘操作。
    """
    instance = getattr(adapter, "_instance", None)
    if instance is None:
        logger.info(
            "[JiuWenClaw][DIAG] prepare_interrupt_resume (module): EARLY RETURN "
            "(adapter._instance is None) request_id=%s session_id=%s",
            getattr(request, "request_id", ""), getattr(request, "session_id", ""),
        )
        return

    session_id = str(getattr(request, "session_id", "") or "").strip()
    if not session_id:
        logger.info(
            "[JiuWenClaw][DIAG] prepare_interrupt_resume (module): EARLY RETURN (no session_id) "
            "request_id=%s",
            getattr(request, "request_id", ""),
        )
        return

    params = request.params if isinstance(getattr(request, "params", None), dict) else None
    if params is None:
        logger.info(
            "[JiuWenClaw][DIAG] prepare_interrupt_resume (module): EARLY RETURN (no params) "
            "request_id=%s session_id=%s",
            getattr(request, "request_id", ""), session_id,
        )
        return

    # dev-stable 实际 params.mode 为 "agent"（统一模式），历史遗留 "agent.plan"。
    # 两者都带 todo 循环，都应注入续跑提示；其余 mode 才跳过。
    mode = str(params.get("mode", "agent.plan") or "agent.plan").strip()
    if mode not in ("agent", "agent.plan"):
        logger.info(
            "[JiuWenClaw][DIAG] prepare_interrupt_resume (module): EARLY RETURN "
            "(mode=%s not in agent/agent.plan) request_id=%s session_id=%s",
            mode, getattr(request, "request_id", ""), session_id,
        )
        return

    query = str(params.get("query", "") or "")
    if not is_resume_user_query(query):
        logger.info(
            "[JiuWenClaw][DIAG] prepare_interrupt_resume (module): EARLY RETURN "
            "(is_resume_user_query=False) query=%r request_id=%s session_id=%s",
            query[:60], getattr(request, "request_id", ""), session_id,
        )
        return
    logger.info(
        "[JiuWenClaw][DIAG] prepare_interrupt_resume (module): is_resume_user_query=True, "
        "proceeding query=%r session_id=%s",
        query[:60], session_id,
    )

    get_modify_tool = getattr(adapter, "_get_todo_modify_tool", None)
    if not callable(get_modify_tool):
        return

    modify_tool = get_modify_tool(session_id)
    if modify_tool is None:
        return

    todos = await load_session_todo_items(modify_tool, session_id)
    if not has_active_todo_items(todos):
        return

    resolve_language = getattr(adapter, "_resolve_runtime_language", None)
    language = resolve_language() if callable(resolve_language) else "cn"

    session = create_agent_session(session_id=session_id, card=instance.card)
    await session.pre_run(inputs=None)
    try:
        # 哨兵：已有其他恢复机制注入则跳过。同时查临时 session（走 checkpointer
        # 的磁盘态）和运行时 session（in-memory 态）—— 同一请求周期内 plan_pause
        # 可能已把哨兵标到 runtime_session 上，临时 session 的 pre_run 不一定能读到。
        if is_interrupt_recovery_injected(session) or (
            runtime_session is not None and is_interrupt_recovery_injected(runtime_session)
        ):
            return

        paused, _snapshot = read_plan_pause_from_session(session)
        if paused:
            return

        snapshot_text = format_todo_snapshot_lines(todos)
        decision = build_interrupt_resume_decision_prompt(
            language,
            snapshot=snapshot_text,
        )
        merge_supplementary_into_request_params(params, decision)
        # 标志同时落临时 session（保 checkpointer 落盘，跨请求兜底）与运行时
        # session（保 before_invoke 这一轮看得见）；两者是不同对象，只标临时
        # session 等价于没标 runtime。
        set_todo_resume_snapshot_pending(session, pending=True)
        mark_interrupt_recovery_injected(session)
        if runtime_session is not None and runtime_session is not session:
            set_todo_resume_snapshot_pending(runtime_session, pending=True)
            mark_interrupt_recovery_injected(runtime_session)
        await post_agent_execute_for_session(session)

        logger.info(
            "[JiuWenClawDeepAdapter] interrupt-resume decision prompt injected session=%s tasks=%d",
            session_id,
            len(todos),
        )
    except Exception as exc:
        logger.warning(
            "[JiuWenClawDeepAdapter] prepare_interrupt_resume_for_request failed session_id=%s: %s",
            session_id,
            exc,
        )
    finally:
        await session.post_run()
