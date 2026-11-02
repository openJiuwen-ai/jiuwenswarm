"""会话 interrupt：状态读写、停工、cancel/supplement/pause/resume。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.harness.goal.schema import GoalStatus

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _append_cancelled_tools_to_history,
    _goal_record_payload,
    _resolve_interrupt_session_id,
)
from jiuwenswarm.server.runtime.agent_adapter.interrupt_state_machine import (
    PHASE_CANCELLED,
    PHASE_SUPPLEMENTED,
    mark_interrupt_cancelled,
    mark_interrupt_supplemented,
)
from jiuwenswarm.server.runtime.agent_adapter.interrupt_resume_helpers import (
    set_todo_resume_snapshot_pending,
)
from jiuwenswarm.server.runtime.agent_adapter.plan_pause_helpers import (
    clear_interrupt_recovery_injected,
    clear_session_interrupt_state,
    post_agent_execute_for_session,
)
from jiuwenswarm.server.runtime.agent_adapter.session_flag_proxy import build_flag_proxy

logger = logging.getLogger(__name__)


def _read_session_interrupt_state(adapter) -> Any:
    """当前交互会话里的工具暂停。没有则返回 None。"""
    instance = getattr(adapter, "_instance", None)
    if instance is None:
        return None
    sessions = (
        getattr(instance, "_interaction_session", None),
        getattr(instance, "_loop_session", None),
    )
    seen: list[Any] = []
    for session in sessions:
        if session is None or any(session is item for item in seen):
            continue
        seen.append(session)
        targets = (session, getattr(session, "_inner", None))
        for target in targets:
            if target is None:
                continue
            getter = getattr(target, "get_state", None)
            if not callable(getter):
                continue
            try:
                state = getter(INTERRUPTION_KEY)
            except Exception:  # noqa: BLE001
                continue
            if state is not None:
                return state
    return None


async def _clear_session_persisted_interrupt_state(
    adapter,
    session_id: str | None,
    *,
    reason: str,
    clear_todo_resume_snapshot_pending: bool = False,
    terminal_phase: str | None = None,
) -> None:
    """清理持久化中断状态；``terminal_phase`` 非空时同时落中断状态机终态。

    cancel/supplement 进入终态（cancelled / supplemented）后，同 session
    的旧卡片应答会被 ``guard_stale_interrupt_response`` 拒绝，不再误入
    runtime 重放（防"点一张生一张"与死卡点击）。
    """
    if not session_id:
        return
    if adapter._instance is None:
        return

    try:
        from openjiuwen.core.session.agent import create_agent_session
        session = create_agent_session(session_id=session_id, card=adapter._instance.card)
        await session.pre_run(inputs=None)
        # 清理/落终态统一走 flag proxy：临时 session（post_agent_execute
        # 落盘 checkpointer）与运行时 _interaction_session（内存态）同时
        # 生效——这些标志标记时均经 proxy 双写，只清临时 session 会在
        # 运行时内存里留下 stale 值（下一条消息仍可能误判 resume）。
        runtime_session = getattr(adapter._instance, "_interaction_session", None)
        flag_proxy = build_flag_proxy(session, runtime_session)
        clear_session_interrupt_state(flag_proxy)
        clear_interrupt_recovery_injected(flag_proxy)
        if clear_todo_resume_snapshot_pending:
            set_todo_resume_snapshot_pending(flag_proxy, pending=False)
        if terminal_phase is not None:
            # 终态同样经 proxy 双写；新卡片发出时会重开为 paused。
            if terminal_phase == PHASE_SUPPLEMENTED:
                mark_interrupt_supplemented(flag_proxy, reason=reason)
            else:
                mark_interrupt_cancelled(flag_proxy, reason=reason)
            # 终态：全部活卡转死卡——后续任何旧卡应答都会被活性卡守卫拒绝。
            adapter._invalidate_all_hitl_card_instances()
        await post_agent_execute_for_session(session, adapter._checkpointer)
        # 同时清理 SkillTurbo 自己的 resume 上下文，避免下次 plain chat 时
        # 误命中"resume 路径"。显式传 card：真实 Session 无公开 card 属性，
        # 隐式提取恒为 None 会退回默认键 fallback，清不到隔离键（R1 引信）。
        try:
            from jiuwenswarm.server.runtime.skill_turbo.resume_context import (
                ResumeContextManager,
            )

            _card = getattr(getattr(adapter, "_instance", None), "card", None)
            await ResumeContextManager(session, card=_card).clear()
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] clear skill_turbo resume ctx failed",
                exc_info=True,
            )
        await session.post_run()
        logger.info(
            "[JiuWenSwarmDeepAdapter] %s: cleared persisted interrupt state session_id=%s",
            reason,
            session_id,
        )
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] %s: clear persisted interrupt state failed session_id=%s error=%s",
            reason,
            session_id,
            exc,
        )


async def _stop_session_interrupt_work(
    adapter,
    session_id: str | None,
    *,
    intent: str,
    reset_for_new_task: bool = False,
) -> list[dict[str, Any]]:
    """Per-session teardown: rail abort, shell kill, cancelled tool collection."""
    sid = _resolve_interrupt_session_id(session_id)
    cancelled_tasks = await adapter._cancel_session_agent_tasks(sid)
    cancelled_tool_results = adapter._collect_cancelled_tools_for_session(
        session_id,
        reset_for_new_task=reset_for_new_task,
    )
    try:
        from openjiuwen.core.sys_operation.shell_process_registry import (
            kill_shell_processes_for_session_tree,
        )

        killed = kill_shell_processes_for_session_tree(sid)
        if killed:
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): killed %d shell process(es) session=%s",
                intent,
                killed,
                sid,
            )
    except Exception:
        logger.debug(
            "[JiuWenSwarmDeepAdapter] interrupt(%s): kill_shell_processes failed",
            intent,
            exc_info=True,
        )
    if cancelled_tasks:
        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt(%s): cancelled %d agent task(s) session=%s",
            intent,
            cancelled_tasks,
            sid,
        )
    return cancelled_tool_results


async def _reattach_interrupt_output(adapter, session_id: str) -> Any | None:
    """Briefly wait for the interrupted consumer to release its output lease."""

    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _INTERRUPT_OUTPUT_ATTACH_RETRY_COUNT,
        _INTERRUPT_OUTPUT_ATTACH_RETRY_INTERVAL_SECONDS,
    )

    started = time.monotonic()
    for attempt in range(1, _INTERRUPT_OUTPUT_ATTACH_RETRY_COUNT + 1):
        await asyncio.sleep(_INTERRUPT_OUTPUT_ATTACH_RETRY_INTERVAL_SECONDS)
        interaction_stream = await adapter._instance.attach_output()
        if interaction_stream is not None:
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt output reattached: "
                "session_id=%s attempts=%s elapsed_ms=%.1f",
                session_id,
                attempt,
                (time.monotonic() - started) * 1000,
            )
            return interaction_stream
    logger.info(
        "[JiuWenSwarmDeepAdapter] interrupt output still leased; returning ACK: "
        "session_id=%s attempts=%s elapsed_ms=%.1f",
        session_id,
        _INTERRUPT_OUTPUT_ATTACH_RETRY_COUNT,
        (time.monotonic() - started) * 1000,
    )
    return None


async def process_interrupt(adapter, request: AgentRequest) -> AgentResponse:
    """处理 interrupt 请求.

    根据 intent 分流：
    - pause: 暂停循环（不取消任务）
    - resume: 恢复已暂停的循环
    - cancel: 为当前 session 生成取消结果与清理信息；真正停任务由 SessionManager 完成
    - supplement: 取消当前任务但保留 todo

    Args:
        request: AgentRequest，params 中可包含：
            - intent: 中断意图 ('pause' | 'cancel' | 'resume' | 'supplement')
            - new_input: 新的用户输入（用于切换任务）

    Returns:
        AgentResponse 包含 interrupt_result 事件数据
    """
    if not adapter._is_session_scoped_adapter:
        if isinstance(request.params, dict):
            intent_for_dispatch = request.params.get("intent", "cancel")
        else:
            intent_for_dispatch = "cancel"
        if intent_for_dispatch in ("cancel", "supplement"):
            # cancel/supplement：若该 session 尚无 session-scoped adapter（即没有 in-flight 流），
            # 则不创建——创建会跑完整 agent 初始化（17 rail + ensure_initialized，
            # 真实场景含 ProjectMemoryRail 扫描，耗时可达数十秒），把 cancel 处理本身卡住，
            # 后续 esc 的 interrupt 全堵在队列里 → AgentServer 响应超时。
            # 拿不到 cached adapter 即"无运行中任务"，直接 fallthrough 到主 adapter 的
            # cancel 逻辑（下方 `not _session_is_active` 分支会跑 per-session teardown 并返回 success）。
            cached = adapter._get_cached_session_adapter(request.session_id)
            if cached is None:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt(%s): no cached session adapter for "
                    "session=%s, skip create (no in-flight stream) — fallthrough to shared teardown",
                    intent_for_dispatch,
                    request.session_id,
                )
            else:
                try:
                    return await cached.process_interrupt(request)
                finally:
                    await adapter._evict_idle_session_adapters()
        else:
            # pause/resume：需要 session-scoped adapter 的 StreamEventRail，按原逻辑创建。
            session_adapter = await adapter._get_or_create_session_adapter(
                request.session_id, request=request
            )
            try:
                return await session_adapter.process_interrupt(request)
            finally:
                await adapter._evict_idle_session_adapters()

    intent = request.params.get("intent", "cancel")
    new_input = request.params.get("new_input")

    # Interaction-managed sessions: route cancel/supplement through
    # DeepAgent.cancel_round instead of the global DeepAgent abort + raw
    # asyncio-task cancellation path.  The global path kills the shared
    # stream producer out-of-band, which leaves the interaction supervisor's
    # session stream consumer hanging forever and stops the
    # backend from responding to any further messages.  The owning output
    # stream closes with abort_active_round=True for stream requests (primary
    # path).  cancel_round below is the idempotent unary fallback: it aborts
    # the current user OR goal attempt but never clears GoalRecord.
    if (
        adapter._instance is not None
        and adapter._instance_interaction_started()
        and intent in ("cancel", "supplement")
    ):
        return await adapter._process_interaction_interrupt(request, intent, new_input)

    # Session guard: only execute interrupt operations if the target session
    # is currently active on this adapter. Without this guard, a shared adapter
    # (cached by mode:sub_mode:project_dir) would abort/pause ALL concurrent
    # sessions when any one session is interrupted.
    # Normalize session_id the same way process_message_*_impl does, so that
    # an empty-string or None request.session_id doesn't bypass the guard.
    _normalized_sid = _resolve_interrupt_session_id(request.session_id)
    _session_is_active = adapter._is_session_active(_normalized_sid)
    if not _session_is_active and intent in ("pause", "resume"):
        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt(%s):",
            "session=%s not active on this adapter, ",
            "skipping pause/resume (active_sessions=%s)",
            intent,
            request.session_id,
            dict(adapter._active_session_ids),
        )
    elif not _session_is_active and intent in ("cancel", "supplement"):
        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt(%s): session=%s not in active counter "
            "(stream may have unwound); still running per-session teardown "
            "(active_sessions=%s)",
            intent,
            request.session_id,
            dict(adapter._active_session_ids),
        )

    success = True
    updated_todos = None
    cancelled_tool_results = []

    if intent == "pause":
        # 暂停：通过 StreamEventRail 在下一个 model_call/tool_call checkpoint 阻塞
        if _session_is_active and adapter._stream_event_rail is not None:
            adapter._stream_event_rail.pause(request.session_id)
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt: 已暂停执行 request_id=%s",
                request.request_id,
            )
        message = "任务已暂停"

    elif intent == "resume":
        # 恢复：解除 StreamEventRail 的 pause 阻塞 + 清除 abort 标志
        if _session_is_active and adapter._stream_event_rail is not None:
            adapter._stream_event_rail.resume(request.session_id)
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt: 已恢复执行 request_id=%s",
                request.request_id,
            )
        message = "任务已恢复"

    elif intent == "supplement":
        # supplement: 停止当前执行，但保留 todo（新任务会根据 todo 待办继续执行）
        session_id = str(request.session_id or "").strip()
        if session_id and adapter._instance is not None and adapter._task_planning_rail is not None:
            try:
                await adapter._freeze_checkpoint_before_abort(
                    session_id, reason="supplement", persist_checkpoint=True
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] interrupt(supplement): checkpoint freeze failed "
                    "session_id=%s: %s (continuing abort)",
                    session_id, exc, exc_info=True,
                )
        cancelled_tool_results = await adapter._stop_session_interrupt_work(
            request.session_id,
            intent="supplement",
        )
        if _session_is_active:
            # Global abort is safe only when this session has work in flight.
            # When inactive, another session may have just started — aborting
            # the shared DeepAgent would kill it as collateral damage.
            await adapter._abort_shared_agent_if_safe(_normalized_sid, "supplement")
        adapter._revoke_main_skill_authorization(
            request.session_id,
            reason="task_supplemented",
        )
        # 清理持久化的中断状态（哨兵 / SkillTurbo resume ctx），但保留 todo；
        # 同时落状态机终态 supplemented —— 旧卡片应答将被守卫拒绝
        await adapter._clear_session_persisted_interrupt_state(
            request.session_id,
            reason="interrupt(supplement)",
            clear_todo_resume_snapshot_pending=True,
            terminal_phase=PHASE_SUPPLEMENTED,
        )
        # 不清理 todo — 保留给新任务继续
        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt(supplement): 已停止执行 request_id=%s",
            request.request_id,
        )
        message = "任务已切换"

    else:
        # cancel（默认）：终止当前正在运行的 agent 任务 + 清理 todos。
        # 必须同时调用 rail.abort() 和 instance.abort()，否则流式模式下
        # DeepAgent 的 _run_task_loop_stream 后台 Task 不会停止
        # （stream_task.cancel() 只取消了 chunk 转发 Task，不影响 _stream_process）。
        # SessionManager.cancel_session_task 仅管理非流式队列 Task，对流式后台 Task 无效。
        session_id = str(request.session_id or "").strip()
        is_plan_mode = adapter._task_planning_rail is not None
        # plan 模式：abort 前 freeze + 落盘，保留 cancel 前 tool/assistant 上下文
        if is_plan_mode and session_id and adapter._instance is not None:
            try:
                await adapter._freeze_checkpoint_before_abort(
                    session_id, reason="cancel", persist_checkpoint=True
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] interrupt(cancel): checkpoint freeze failed "
                    "session_id=%s: %s (continuing abort)",
                    session_id, exc, exc_info=True,
                )
        cancelled_tool_results = await adapter._stop_session_interrupt_work(
            request.session_id,
            intent="cancel",
            reset_for_new_task=True,
        )
        if _session_is_active:
            # Global abort is safe only when this session has work in flight.
            # When inactive, another session may have just started — aborting
            # the shared DeepAgent would kill it as collateral damage.
            await adapter._abort_shared_agent_if_safe(_normalized_sid, "cancel")
        adapter._revoke_main_skill_authorization(
            request.session_id,
            reason="task_cancelled",
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt(cancel): 已设置 abort 并解除 pause 阻塞"
        )

        updated_todos = None
        if request.session_id:
            try:
                updated_todos = await adapter._cancel_pending_todos(request.session_id)
            except Exception as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] 标记 todo cancelled 失败: %s", exc)

            # 清理持久化的中断状态（哨兵 / plan_pause / SkillTurbo resume ctx）；
            # 同时落状态机终态 cancelled —— 旧卡片应答将被守卫拒绝
            await adapter._clear_session_persisted_interrupt_state(
                request.session_id,
                reason="interrupt(cancel)",
                clear_todo_resume_snapshot_pending=True,
                terminal_phase=PHASE_CANCELLED,
            )

            # Cancel auto_harness active run if exists
            try:
                if adapter._auto_harness_service is not None \
                    and adapter._auto_harness_service.has_active_run(request.session_id):
                    adapter._auto_harness_service.cancel_session_run(request.session_id)
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] interrupt(cancel): cancelled auto_harness run for session=%s",
                        request.session_id,
                    )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] Failed to cancel auto_harness run: %s",
                    exc,
                )

        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt(cancel): 已停止执行 request_id=%s",
            request.request_id,
        )
        if new_input:
            message = "已切换到新任务"
        else:
            message = "任务已取消"

    # SkillTurbo HITL 终止语义：cancel/supplement 在终止 executor
    # （_stop_session_interrupt_work + _abort_shared_agent_if_safe）之后
    # 再清 pending 的 skill_acceleration_exec interrupt 状态（产物保留）。
    # 顺序不能反：fresh 执行中被 cancel 时 executor 可能在退出前落盘
    # resume_ctx（HITL 中断点写入），先清会被覆盖，下一条消息仍命中
    # 残留断点重放被取消任务。
    if intent in ("cancel", "supplement"):
        await adapter._clear_pending_skill_turbo_hitl(request.session_id)

    payload = {
        "event_type": "chat.interrupt_result",
        "intent": intent,
        "success": success,
        "message": message,
    }

    if new_input:
        payload["new_input"] = new_input

    # cancel 后附带更新的 todo 列表，通知前端刷新
    if intent not in ("pause", "resume", "supplement") and updated_todos is not None:
        payload["todos"] = updated_todos

    # cancel 后附带被中断的工具执行结果，通知前端更新状态
    if cancelled_tool_results:
        payload["cancelled_tools"] = cancelled_tool_results
        # 写入历史记录，确保刷新网页后工具状态正确显示
        _append_cancelled_tools_to_history(request, cancelled_tool_results)

    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )


async def _process_interaction_interrupt(
    adapter,
    request: "AgentRequest",
    intent: str,
    new_input: Any,
) -> "AgentResponse":
    """Handle cancel/supplement for an interaction-managed session.

    Closing the owning interaction output stream is the primary shutdown
    path for streaming hosts.  This unary handler is the idempotent
    fallback: ``cancel_round`` aborts the current user or goal attempt
    without clearing GoalRecord.  For user cancel, pause an ACTIVE goal
    first so the GoalBar stops continuing after the round is aborted.
    """
    paused_goal_payload: dict[str, Any] | None = None
    if intent == "cancel":
        try:
            goal_manager = adapter._get_goal_manager()
            if goal_manager is not None:
                record = await goal_manager.get()
                status = getattr(record, "status", None) if record is not None else None
                if status is GoalStatus.ACTIVE:
                    paused = await goal_manager.pause()
                    paused_goal_payload = _goal_record_payload(paused)
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] interrupt(cancel): paused ACTIVE goal "
                        "session=%s",
                        request.session_id,
                    )
        except Exception:
            logger.exception(
                "[JiuWenSwarmDeepAdapter] interrupt(cancel): goal pause failed "
                "session=%s",
                request.session_id,
            )

    cancelled = False
    # Collect in-flight tools without cancelling the stream producer task —
    # interaction cancel must keep round ownership in cancel_round().
    cancelled_tool_results = adapter._collect_cancelled_tools_for_session(
        request.session_id,
        reset_for_new_task=(intent == "cancel"),
    )
    try:
        cancelled = await adapter._instance.cancel_round(
            reason="user_cancel",
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt(%s): interaction round cancel "
            "cancelled=%s session=%s",
            intent,
            cancelled,
            request.session_id,
        )
    except Exception:
        logger.exception(
            "[JiuWenSwarmDeepAdapter] interrupt(%s): interaction cancel failed",
            intent,
        )
    # Interaction-managed 路径是流式宿主的主取消路径，同样回收动态授权。
    adapter._revoke_main_skill_authorization(
        request.session_id,
        reason="task_supplemented" if intent == "supplement" else "task_cancelled",
    )
    # cancel 同样要 discard 暂停在 ask_user interrupt 上的纯 ask_user 轮：
    # cancel_round 只中止"正在跑的 attempt"、不动暂停态 round ownership（故
    # cancelled=False），残留的 _loop_session.INTERRUPTION_KEY 会让下一条
    # chat.send 被当 resume 输入塞进 rail.resolve_interrupt，非 dict 的"再改改"
    # parse 失败被当 skip 了结 → 不弹卡直接重生成（Bug2）。复用 supplement 的
    # 清理：内存 INTERRUPTION_KEY 置 None + pop 掉 pending ask_user AI 消息。
    # 非纯 ask_user 轮 / 无 pending 时方法自带守卫返回 False，零回归。
    should_drop_ask_user = intent == "cancel" or (
        intent == "supplement" and isinstance(new_input, str) and new_input.strip()
    )
    if should_drop_ask_user:
        await adapter._drop_pending_pure_ask_user_round(request.session_id)
    # 与非 interaction 路径对齐：清理持久化中断状态（哨兵 / plan_pause /
    # SkillTurbo resume ctx）并落状态机终态，否则取消后残留的
    # INTERRUPTION_KEY / resume_ctx 会把下一条消息误判为 resume 输入，
    # 且旧卡片应答不会被守卫拒绝（历史死循环 bug 的根因之一）。
    if request.session_id:
        await adapter._clear_session_persisted_interrupt_state(
            request.session_id,
            reason=f"interrupt({intent})",
            clear_todo_resume_snapshot_pending=True,
            terminal_phase=(
                PHASE_SUPPLEMENTED if intent == "supplement" else PHASE_CANCELLED
            ),
        )
    # SkillTurbo HITL 终止语义：cancel_round 终止 round 之后再清 pending 的
    # skill_acceleration_exec interrupt 状态（产物保留）。顺序不能反：
    # fresh 执行中被 cancel 时 executor 可能在退出前落盘 resume_ctx
    # （HITL 中断点写入），先清会被覆盖，下一条消息仍命中残留断点重放
    # 被取消任务。
    await adapter._clear_pending_skill_turbo_hitl(request.session_id)
    message = "任务已切换" if intent == "supplement" else "任务已取消"

    payload: dict[str, Any] = {
        "event_type": "chat.interrupt_result",
        "intent": intent,
        "success": True,
        "message": message,
    }
    if new_input:
        payload["new_input"] = new_input
    if paused_goal_payload is not None:
        # Always return the paused snapshot on the interrupt response so
        # Web/TUI can refresh GoalBar even when no output lease remains
        # to receive goal.updated.
        payload["goal"] = paused_goal_payload
    if cancelled_tool_results:
        payload["cancelled_tools"] = cancelled_tool_results
        _append_cancelled_tools_to_history(request, cancelled_tool_results)

    # Best-effort todo cancellation for user cancel (does not touch runtime).
    if cancelled and intent == "cancel" and request.session_id:
        try:
            updated_todos = await adapter._cancel_pending_todos(request.session_id)
            if updated_todos is not None:
                payload["todos"] = updated_todos
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] 标记 todo cancelled 失败: %s", exc,
            )

    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )
