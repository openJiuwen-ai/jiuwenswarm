# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SDK interaction lifecycle and authorized input/output transactions.

Session assembly owns selecting an adapter. This driver operates only on that
selected owner: attaching output precedes send, permission reservations precede
SDK dispatch, and failure releases both the reservation and output lease.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from openjiuwen.harness.goal.schema import GoalStatus
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.server.runtime.agent_adapter.permission_dispatch import (
    ROOT_PERMISSION_ANSWER_KEY as _ROOT_PERMISSION_ANSWER_KEY,
    ROOT_PERMISSION_HANDOFF_KEY as _ROOT_PERMISSION_HANDOFF_KEY,
    RootPermissionDispatchHandoff,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionAnswer,
    RootPermissionQueueError,
)


logger = logging.getLogger(__name__)


class SDKExecutionDriver:
    """Operate one adapter's SDK interaction without owning another Session."""

    def __init__(self, backend: Any, *, create_session: Callable[..., Any]) -> None:
        self._owner = backend
        self._create_session = create_session

    async def start(self, session_id: str) -> None:
        """Bind a product Session and start this adapter's DeepAgent interaction loop.

        Public entry for the facade/session-pool path. A missing instance or
        failed readiness check propagates so the warm pool cannot publish a
        partially initialized slot.
        """
        owner = self._owner
        if getattr(owner, "_external_harness_binding", None) is not None:
            return
        if owner._instance is None:
            raise RuntimeError("DeepAgent instance is not initialized")

        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime import (
            get_kv_cache_runtime,
        )

        session = self._create_session(
            session_id=session_id,
            card=getattr(owner._instance, "card", None),
            kv_cache_runtime=get_kv_cache_runtime(),
        )
        await session.pre_run(inputs={})
        await owner.install_session_input_guard()
        if session_id.startswith("managed-task-"):
            await owner.install_voice_task_rail()
        await owner._instance.start(session=session)
        if getattr(owner._instance, "_interaction_started", True) is not True:
            raise RuntimeError(
                f"DeepAgent interaction did not become ready: {session_id}"
            )
        logger.info(
            "[JiuWenSwarmDeepAdapter] start completed: session_id=%s",
            session_id,
        )

    async def stop(self) -> None:
        """Stop this adapter's DeepAgent interaction loop if it was started."""
        owner = self._owner
        if owner._instance is None:
            return
        session = (
            owner._instance.loop_session
            if owner._channel_id == "process_cli"
            and getattr(owner._instance, "interaction_started", False) is True
            else None
        )
        await owner._instance.stop()
        if session is not None:
            # The product owns this Session's lifecycle. Persist cleared HITL
            # state after the loop stops, before this one-shot process exits.
            # Otherwise the next process can replay an already approved tool.
            await session.post_run()

    async def attach_and_send(
        self,
        request: AgentRequest,
        inputs: dict[str, Any],
        *,
        send_without_output: bool,
    ) -> tuple[Any | None, bool]:
        owner = self._owner
        answer = inputs.get(_ROOT_PERMISSION_ANSWER_KEY)
        if answer is not None and not isinstance(answer, RootPermissionAnswer):
            raise RootPermissionQueueError("permission_queue_answer_invalid")
        handoff = inputs.get(_ROOT_PERMISSION_HANDOFF_KEY)
        if owner._enable_auto_permission and not isinstance(
            handoff, RootPermissionDispatchHandoff
        ):
            raise RootPermissionQueueError("permission_dispatch_handoff_missing")
        stream = None
        try:
            # Plain interrupt rounds finish their output before a resume starts.
            # An ACK-only inject into a lease with EOF already queued can lose
            # all resumed output. Goal readers and permission-queue callbacks
            # remain live owners and must keep their existing injection path.
            previous = getattr(owner, "_interaction_output_handoff", None)
            if previous is not None and previous.owner is owner._instance:
                if (
                    owner._is_interrupt_resume_dispatch(request.params)
                    and answer is None
                    and not owner._goal_record_is_active()
                ):
                    await previous.wait()
            stream = await owner._instance.attach_output()
            if stream is None and (answer is not None or not send_without_output):
                if answer is not None:
                    raise RootPermissionQueueError(
                        "permission_queue_output_unavailable"
                    )
                return None, False
            if (
                stream is not None
                and answer is None
                and owner._should_refresh_cron_web_context(request, send_without_output=send_without_output)
            ):
                # Web may have cached this session before its cron run completed.
                # Restore that run once before accepting the first Web follow-up.
                try:
                    from jiuwenswarm.agents.harness.common.session_ops_service import (
                        warmup_session_context,
                    )

                    owner._cron_web_context_refreshed = await warmup_session_context(
                        deep_agent=owner._instance,
                        session_id=request.session_id,
                        history_before_request_id=request.request_id,
                        refresh_existing=True,
                    )
                    if not owner._cron_web_context_refreshed:
                        raise RuntimeError("定时任务历史恢复失败，请重试。")
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] cron Web context refresh failed: "
                        "session_id=%s error=%s", request.session_id, exc,
                    )
                    raise
            if stream is not None:
                from jiuwenswarm.server.runtime.agent_adapter.output_handoff import (
                    OutputHandoff,
                )

                stream = OutputHandoff(owner._instance, stream)
                owner._interaction_output_handoff = stream
            mode = owner._resolve_input_dispatch_mode(request.params)
            from jiuwenswarm.runtime.execution_telemetry import observe_execution_entry

            observe_execution_entry(
                request,
                str(
                    (request.params or {}).get("mode")
                    or getattr(owner, "_session_instance_mode", "agent")
                ),
            )
            dispatched = await owner._send_input_with_permission_resume_guard(
                SendInputRequest(
                    request_id=request.request_id,
                    inputs=owner._permission_inputs_for_dispatch(request, inputs, mode),
                    mode=mode,
                )
            )
            return stream, dispatched
        except BaseException:
            owner._permission_dispatch.release(inputs)
            if stream is not None:
                try:
                    await stream.close(abort_active_round=False)
                except BaseException:
                    logger.exception(
                        "[JiuWenSwarmDeepAdapter] output lease close failed"
                    )
            raise

    async def send(
        self,
        request: SendInputRequest,
        *,
        send: Any = None,
    ) -> bool:
        owner = self._owner
        sender = send if send is not None else owner._instance.send_input
        # Source and frozen acceptance use the same boundary after permission
        # dispatch has prepared the actual SDK input. Custom continuation
        # senders are not labelled as a DeepAgent.send_input measurement.
        from jiuwenswarm.runtime.execution_telemetry import (
            observe_sdk_entry,
            sdk_entry_sink,
        )

        if send is None and (
            sdk_entry_sink.get() is not None
            or os.environ.get("JIUWENSWARM_SDK_ENTRY_LOG")
        ):

            async def measured_sender(actual_request: SendInputRequest) -> Any:
                observe_sdk_entry(actual_request, sender)
                return await sender(actual_request)

            sdk_sender = measured_sender
        else:
            sdk_sender = sender
        if not owner._enable_auto_permission:
            await sdk_sender(request)
            return False
        return await owner._permission_dispatch.send(request, sdk_sender)

    async def interrupt(self, request: AgentRequest) -> AgentResponse:
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
        owner = self._owner
        if not owner._is_session_scoped_adapter:
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
                cached = owner._get_cached_session_adapter(request.session_id)
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
                        await owner._evict_idle_session_adapters()
            else:
                # pause/resume：需要 session-scoped adapter 的 StreamEventRail，按原逻辑创建。
                session_adapter = await owner._get_or_create_session_adapter(
                    request.session_id, retain_moa_snapshot=True,
                    **(
                        {"model_selection": request.params["model_selection"]}
                        if request.params.get("model_selection") is not None else {}
                    ),
                    **({"moa_enabled": request.params["moa_enabled"]} if "moa_enabled" in request.params else {}),
                    **owner.moa_access_kwargs(request),
                )
                try:
                    return await session_adapter.process_interrupt(request)
                finally:
                    await owner._evict_idle_session_adapters()

        if getattr(self, "_external_harness_binding", None) is not None:
            from jiuwenswarm.server.runtime.agent_adapter.external_harness_helpers import (
                process_external_harness_interrupt,
            )

            return await process_external_harness_interrupt(self, request)

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
            owner._instance is not None
            and owner._instance_interaction_started()
            and intent in ("cancel", "supplement")
        ):
            return await owner._process_interaction_interrupt(
                request, intent, new_input
            )

        # Session guard: only execute interrupt operations if the target session
        # is currently active on this adapter. Without this guard, a shared adapter
        # (cached by mode:sub_mode:project_dir) would abort/pause ALL concurrent
        # sessions when any one session is interrupted.
        # Normalize session_id the same way process_message_*_impl does, so that
        # an empty-string or None request.session_id doesn't bypass the guard.
        _normalized_sid = owner._resolve_interrupt_session_id(request.session_id)
        _session_is_active = owner._is_session_active(_normalized_sid)
        # 只有 pause 受活跃守卫限制：跳过 pause 无害（暂停只是不生效）。
        # resume 不受限：它幂等且无害，若因会话不在活跃计数里被跳过，
        # 已阻塞的 pause latch 将永久无人解除（cron 挂死 59 分钟的机制之一）。
        if not _session_is_active and intent == "pause":
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): session=%s not active on "
                "this adapter, skipping pause (active_sessions=%s)",
                intent,
                request.session_id,
                dict(owner._active_session_ids),
            )
        elif not _session_is_active and intent in ("cancel", "supplement"):
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): session=%s not in active counter "
                "(stream may have unwound); still running per-session teardown "
                "(active_sessions=%s)",
                intent,
                request.session_id,
                dict(owner._active_session_ids),
            )

        success = True
        updated_todos = None
        cancelled_tool_results = []
        cutover_handoff: RootPermissionDispatchHandoff | None = None
        continuation_discarded = True

        if intent == "pause":
            # 暂停：通过 StreamEventRail 在下一个 model_call/tool_call checkpoint 阻塞。
            # 下发必须用与守卫一致的 _normalized_sid，否则 strip/空值差异会让
            # pause 落到别的 key 上，resume 永远对不上号。
            if _session_is_active and owner._stream_event_rail is not None:
                owner._stream_event_rail.pause(_normalized_sid)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt: 已暂停执行 request_id=%s",
                    request.request_id,
                )
            message = "任务已暂停"

        elif intent == "resume":
            # 恢复：解除 StreamEventRail 的 pause 阻塞 + 清除 abort 标志。
            # 不检查 _session_is_active：resume 到达时会话可能已离开活跃计数
            # （stream 已回卷），跳过它会让 latch 卡死；resume 本身幂等无害。
            if owner._stream_event_rail is not None:
                owner._stream_event_rail.resume(_normalized_sid)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt: 已恢复执行 request_id=%s"
                    " (session_active=%s)",
                    request.request_id,
                    _session_is_active,
                )
            message = "任务已恢复"

        elif intent == "supplement":
            # supplement: 停止当前执行，但保留 todo（新任务会根据 todo 待办继续执行）
            cutover_handoff = await owner._permission_dispatch.start_cutover(
                _normalized_sid
            )
            (
                cancelled_tool_results,
                continuation_discarded,
            ) = await owner._stop_session_interrupt_work(
                request.session_id,
                intent="supplement",
            )
            if _session_is_active:
                # Global abort is safe only when this session has work in flight.
                # When inactive, another session may have just started — aborting
                # the shared DeepAgent would kill it as collateral damage.
                continuation_discarded = (
                    await owner._abort_shared_agent_if_safe(
                        _normalized_sid, "supplement"
                    )
                    or continuation_discarded
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
            cutover_handoff = await owner._permission_dispatch.start_cutover(
                _normalized_sid
            )
            (
                cancelled_tool_results,
                continuation_discarded,
            ) = await owner._stop_session_interrupt_work(
                request.session_id,
                intent="cancel",
                reset_for_new_task=True,
            )
            if _session_is_active:
                # Global abort is safe only when this session has work in flight.
                # When inactive, another session may have just started — aborting
                # the shared DeepAgent would kill it as collateral damage.
                continuation_discarded = (
                    await owner._abort_shared_agent_if_safe(_normalized_sid, "cancel")
                    or continuation_discarded
                )
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(cancel): 已设置 abort 并解除 pause 阻塞"
            )

            updated_todos = None
            if request.session_id:
                try:
                    updated_todos = await owner._cancel_pending_todos(
                        request.session_id
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] 标记 todo cancelled 失败: %s", exc
                    )

                # Cancel auto_harness active run if exists
                try:
                    if (
                        owner._auto_harness_service is not None
                        and owner._auto_harness_service.has_active_run(
                            request.session_id
                        )
                    ):
                        owner._auto_harness_service.cancel_session_run(
                            request.session_id
                        )
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

        payload = {
            "event_type": "chat.interrupt_result",
            "intent": intent,
            "success": success,
            "message": message,
        }

        if new_input:
            payload["new_input"] = new_input

        # cancel 后附带更新的 todo 列表，通知前端刷新
        if (
            intent not in ("pause", "resume", "supplement")
            and updated_todos is not None
        ):
            payload["todos"] = updated_todos

        # cancel 后附带被中断的工具执行结果，通知前端更新状态
        if cancelled_tool_results:
            payload["cancelled_tools"] = cancelled_tool_results
            # 写入历史记录，确保刷新网页后工具状态正确显示
            await owner._append_cancelled_tools_to_history(
                request, cancelled_tool_results
            )
        if cutover_handoff is not None:
            try:
                continuation_discarded = (
                    await owner._permission_dispatch.complete_cutover(
                        cutover_handoff,
                        discard=lambda keys: (
                            owner._discard_frozen_permission_continuation(
                                request.session_id, keys
                            )
                        ),
                        discard_confirmed=continuation_discarded,
                        keep_lock=False,
                    )
                )
            except RootPermissionQueueError:
                continuation_discarded = False
            if not continuation_discarded:
                payload["success"] = False
                payload["error"] = "permission_continuation_discard_failed"

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def interrupt_interaction(
        self,
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
        owner = self._owner
        root_session_id = owner._resolve_interrupt_session_id(request.session_id)
        paused_goal_payload: dict[str, Any] | None = None
        if intent == "cancel":
            try:
                goal_manager = owner._get_goal_manager()
                if goal_manager is not None:
                    record = await goal_manager.get()
                    status = (
                        getattr(record, "status", None) if record is not None else None
                    )
                    if status is GoalStatus.ACTIVE:
                        paused = await goal_manager.pause()
                        paused_goal_payload = owner._goal_record_payload(paused)
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

        cutover_handoff = await owner._permission_dispatch.start_cutover(
            root_session_id
        )

        cancelled = False
        cancel_call_completed = False
        # Stop the scheduler execution before asking the interaction owner to
        # cancel the round.  In particular, task_tool awaits its subagent in
        # the scheduler's exec task; cancelling only the interaction round can
        # otherwise block behind that child work and never return an
        # interrupt_result to the TUI.  This does not cancel the registered
        # session stream producer -- cancel_round still owns that lifecycle.
        owner._cancel_scheduler_running_tasks()
        # Collect in-flight tools without cancelling the stream producer task —
        # interaction cancel must keep round ownership in cancel_round().
        cancelled_tool_results = owner._collect_cancelled_tools_for_session(
            request.session_id,
            reset_for_new_task=(intent == "cancel"),
        )
        try:
            cancelled = await owner._instance.cancel_round(
                reason="user_cancel",
            )
            cancel_call_completed = True
            if request.channel_id == "video_tool":
                await owner._get_hook_services().close_task_output(
                    owner._voice_agent_task_rail, request
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
        finally:
            # A scheduler task may be installed while cancel_round is
            # unwinding; repeat the targeted cancellation as a safety net.
            owner._cancel_scheduler_running_tasks()
        continuation_discarded = True
        if cutover_handoff is not None:
            try:
                continuation_discarded = (
                    await owner._permission_dispatch.complete_cutover(
                        cutover_handoff,
                        discard=lambda keys: (
                            owner._discard_frozen_permission_continuation(
                                request.session_id, keys
                            )
                        ),
                        discard_confirmed=cancel_call_completed,
                        keep_lock=False,
                    )
                )
            except RootPermissionQueueError:
                continuation_discarded = False
        if intent == "supplement" and isinstance(new_input, str) and new_input.strip():
            await owner._clear_pending_ask_user_interrupt_for_supplement(
                request.session_id
            )
        message = "任务已切换" if intent == "supplement" else "任务已取消"

        payload: dict[str, Any] = {
            "event_type": "chat.interrupt_result",
            "intent": intent,
            "success": continuation_discarded,
            "message": message,
        }
        if not continuation_discarded:
            payload["error"] = "permission_continuation_discard_failed"
        if new_input:
            payload["new_input"] = new_input
        if paused_goal_payload is not None:
            # Always return the paused snapshot on the interrupt response so
            # Web/TUI can refresh GoalBar even when no output lease remains
            # to receive goal.updated.
            payload["goal"] = paused_goal_payload
        if cancelled_tool_results:
            payload["cancelled_tools"] = cancelled_tool_results
            await owner._append_cancelled_tools_to_history(
                request, cancelled_tool_results
            )
        # Best-effort todo cancellation for user cancel (does not touch runtime).
        if cancelled and intent == "cancel" and request.session_id:
            try:
                updated_todos = await owner._cancel_pending_todos(request.session_id)
                if updated_todos is not None:
                    payload["todos"] = updated_todos
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] 标记 todo cancelled 失败: %s",
                    exc,
                )

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=continuation_discarded,
            payload=payload,
            metadata=request.metadata,
        )

    async def deliver_active(
        self, request: AgentRequest, inputs: dict[str, Any]
    ) -> bool:
        """Send through the existing permission transaction and output owner.

        Return False only before sending, when a new output owner is needed.
        Literal '/...' supplements remain text rather than slash commands.
        """
        owner = self._owner
        from jiuwenswarm.server.runtime.agent_adapter.session_input import (
            enqueue_bound_session_input,
            sdk_input_mode,
        )

        from jiuwenswarm.runtime.session_input import SessionInputRejectedError

        mode = sdk_input_mode(request.params)
        cross_session_steer = mode is InputDispatchMode.STEER and isinstance(
            request.params.get(SESSION_MESSAGE_INTERNAL_KEY), dict
        )
        if cross_session_steer:
            owner._require_cross_session_task_admission(request)
        instance = owner._instance
        if instance is None or instance.active_round is None:
            return False
        if not instance.has_output_stream():
            return False
        if owner._stream_completion_state(had_interaction=False) == "suspended":
            raise SessionInputRejectedError(
                "session is waiting for an interaction answer; "
                "supplemental input was not sent"
            )
        bound_round = instance.active_round

        def require_open_input() -> None:
            if cross_session_steer:
                owner._require_cross_session_task_admission(request)
            if mode is InputDispatchMode.STEER:
                guard = owner._session_input_guard
                if guard is None or guard.owner is not instance:
                    accepting = False
                else:
                    accepting = guard.accepting
                if not accepting:
                    raise SessionInputRejectedError(
                        "session is finishing or changing execution state; "
                        "supplemental input was not sent, "
                        "retry after it settles"
                    )

        require_open_input()
        prepared = await owner._prepare_root_input_dispatch(request, inputs)
        try:
            if instance.active_round is None or not instance.has_output_stream():
                return False

            async def send(sdk_request: SendInputRequest) -> None:
                require_open_input()
                if mode is InputDispatchMode.STEER:
                    entry = enqueue_bound_session_input(
                        instance, bound_round, request, sdk_request
                    )
                    await owner._session_input_guard.publish_input_received(entry)
                    return
                await instance.send_input(sdk_request)

            await owner._send_input_with_permission_resume_guard(
                SendInputRequest(
                    request_id=request.request_id,
                    inputs=owner._permission_inputs_for_dispatch(
                        request, prepared, mode
                    ),
                    mode=mode,
                ),
                send=send,
            )
            return True
        finally:
            owner._permission_dispatch.finalize(prepared)
