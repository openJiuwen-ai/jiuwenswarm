# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose request preparation, execution, projection and durable history.

This module has no import of the public facade or extension registry. The facade
supplies routing and compatibility policy through explicit ports; the execution
driver owns SDK output lifetime and each request owns projector/committer state.
"""

from __future__ import annotations

import logging
import asyncio
import time
from dataclasses import dataclass
from contextlib import aclosing
from typing import Any, AsyncIterator, Callable

from gateway.common.mode_matrix import is_team_mode as is_team_runtime_mode
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session.model import TEAM_EXECUTION_CONTEXT_METADATA_KEY
from jiuwenswarm.server.runtime.agent_adapter.user_turn import TEAM_USER_TURN_KEY
from jiuwenswarm.server.runtime.agent_adapter.management_routes import _SKILLDEV_METHODS


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamExecutionPorts:
    """Policy and side-effect ports resolved by the composition boundary."""

    restore_chat_send_equipment_params: Callable[..., Any]
    compute_chat_send_mcp_needed: Callable[..., Any]
    append_compact_history_from_payload: Callable[..., Any]
    history_user_content: Callable[..., Any]
    history_user_extra: Callable[..., Any]
    make_a2ui_final_chunk: Callable[..., Any]
    make_a2ui_pending_render_chunk: Callable[..., Any]
    normalize_nested_stream_chunk: Callable[..., Any]
    persist_ask_user_answer_history: Callable[..., Any]
    resolve_final_record_timestamp: Callable[..., Any]
    run_history_io: Callable[..., Any]
    should_defer_a2ui_processing_status: Callable[..., Any]
    should_probe_a2ui_stream: Callable[..., Any]
    should_record_user_history: Callable[..., Any]
    split_a2ui_stream_content: Callable[..., Any]
    extend_a2ui_stream_probe: Callable[..., Any]
    trigger_auto_memory_extraction: Callable[..., Any]
    with_cross_session_history_metadata: Callable[..., Any]
    with_heartbeat_history_metadata: Callable[..., Any]
    with_web_agent_template_metadata: Callable[..., Any]
    with_web_agent_template_payload: Callable[..., Any]
    append_history_record: Callable[..., Any]
    finalize_assistant_response_if_a2ui: Callable[..., Any]
    get_config: Callable[..., Any]
    is_auto_memory_enabled: Callable[..., Any]
    is_memory_enabled: Callable[..., Any]
    TeamA2UIBlockBuffer: Callable[..., Any]
    TeamPlanApprovalPayloadError: Callable[..., Any]
    person_drove_turn: Callable
    complete_history_request: Callable
    session_message_owns_completion: Callable
    await_history_receipt: Callable


class StreamExecutionService:
    def __init__(self, facade: Any, ports: StreamExecutionPorts) -> None:
        self._facade = facade
        self._ports = ports

    async def run(self, request: AgentRequest) -> AsyncIterator[AgentResponseChunk]:
        """处理流式请求.

        支持多 session 并发执行，同 session 内任务按先进后出顺序执行.
        """
        facade = self._facade
        ports = self._ports
        # Streaming command.goal: get/pause/clear stay one-shot; set/resume
        # continue into the DeepAdapter attach→set/resume→read path below.
        if request.req_method == ReqMethod.COMMAND_GOAL:
            params = request.params if isinstance(request.params, dict) else {}
            action = str(params.get("action", "get") or "get").strip().lower()
            if action not in {"set", "resume"}:
                try:
                    adapter = facade._ensure_adapter(
                        mode=facade._adapter_mode_for_request(request)
                    )
                    session_id = facade._session_manager.get_session_id(
                        request.session_id
                    )
                    goal_result = await adapter.handle_goal_command_structured(
                        params, session_id
                    )
                    if goal_result is None:
                        yield AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            payload={
                                "event_type": "chat.error",
                                "error": "Goal command not handled",
                            },
                            is_complete=True,
                        )
                        return
                    result_type = goal_result.get("result_type")
                    if result_type == "goal_error":
                        yield AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            payload={
                                "event_type": "chat.error",
                                "code": goal_result.get("error_code", "goal_error"),
                                "error": goal_result.get(
                                    "error", "goal operation failed"
                                ),
                                "goal": goal_result.get("goal"),
                            },
                            is_complete=True,
                        )
                        return
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={
                            "event_type": "goal.snapshot",
                            "action": goal_result.get("action", action),
                            "goal": goal_result.get("goal"),
                            "cleared_goal": goal_result.get("cleared_goal"),
                            "message": goal_result.get("output", ""),
                        },
                        is_complete=True,
                    )
                except Exception as exc:
                    logger.exception(
                        "[JiuWenSwarm] streaming COMMAND_GOAL failed: request_id=%s error=%s",
                        request.request_id,
                        exc,
                    )
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={
                            "event_type": "chat.error",
                            "error": f"Goal command error: {exc}",
                        },
                        is_complete=True,
                    )
                return
            # set/resume: fall through to streaming adapter (no user-history write).

        # SkillDev 流式请求：直接委托给 SkillDevService，绕过 ReActAgent
        if request.req_method in _SKILLDEV_METHODS:
            service = facade._get_skilldev_service()
            try:
                async for chunk in service.handle(request):
                    yield chunk
            except Exception as exc:
                logger.error("[JiuWenSwarm] skilldev 流式请求处理失败: %s", exc)
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "skilldev.error", "error": str(exc)},
                    is_complete=True,
                )
            return

        # 无状态 RPC（skills / plugins / Skill Graph）不需要 adapter，
        # 委托给非流式 handler 并包装为单个 chunk，避免触发 adapter 懒初始化。
        # skilldev 已由上面的流式分支处理，这里不会再命中。
        for stateless_handler in (
            facade._handle_skills_request,
            facade._handle_plugins_request,
            facade._handle_package_catalog_request,
        ):
            stateless_response = await stateless_handler(request)
            if stateless_response is not None:
                if stateless_response.ok:
                    chunk_payload = stateless_response.payload
                else:
                    # handler 内部捕获异常并以 ok=False 返回时，注入 chat.error
                    # 事件标记，使下游能识别为错误（与非流式 resp.ok 传播等价）。
                    raw_payload = stateless_response.payload
                    error_msg = (
                        raw_payload.get("error", "unknown error")
                        if isinstance(raw_payload, dict)
                        else "unknown error"
                    )
                    chunk_payload = {"event_type": "chat.error", "error": error_msg}
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload=chunk_payload,
                    is_complete=True,
                )
                return

        adapter = facade._ensure_adapter(mode=facade._adapter_mode_for_request(request))
        validator = getattr(adapter, "validate_auto_permission_workspace_request", None)
        if callable(validator):
            validator(request)

        session_id = facade._session_manager.get_session_id(request.session_id)
        if isinstance(request.params, dict):
            ports.restore_chat_send_equipment_params(session_id, request.params)
        query = request.params.get("query", "")

        mode = (
            request.params.get("mode", "") if isinstance(request.params, dict) else ""
        )
        team_flag = (
            request.params.get("team", False)
            if isinstance(request.params, dict)
            else False
        )
        is_team_mode = team_flag or is_team_runtime_mode(mode)
        from jiuwenswarm.server.runtime.agent_adapter.request_completion import RequestCompletion
        completion = RequestCompletion(request, session_id, is_team_mode=is_team_mode, ports=ports)
        try:
            # proactive_recommendation 是系统触发的推荐指令（不是用户说的话），不写 user
            # history——否则刷新页面会显示"[主动推荐指令] xxx"这种用户没说过的消息。
            # command.goal set history is written only after a successful set inside
            # the DeepAdapter stream path (same success gate as unary process_message).
            params_for_history = request.params if isinstance(request.params, dict) else {}
            history_channel_metadata = (
                dict(request.metadata) if isinstance(request.metadata, dict) else None
            )
            if history_channel_metadata is not None:
                history_channel_metadata.pop(
                    TEAM_EXECUTION_CONTEXT_METADATA_KEY,
                    None,
                )
            if (
                request.req_method != ReqMethod.COMMAND_GOAL
                and ports.should_record_user_history(params_for_history)
            ):
                await ports.run_history_io(
                    ports.append_history_record,
                    session_id=session_id,
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    role="user",
                    content=ports.history_user_content(params_for_history, query),
                    timestamp=time.time(),
                    extra=ports.history_user_extra(params_for_history),
                    channel_metadata=history_channel_metadata,
                    mode=params_for_history.get("mode", "unknown"),
                )
            logger.info(
                "[JiuWenSwarm] 处理流式请求: request_id=%s channel_id=%s session_id=%s sdk=%s",
                request.request_id,
                request.channel_id,
                session_id,
                facade._sdk_name,
            )

            rid = request.request_id
            cid = request.channel_id

            try:
                inputs, memory_mode, user_turn = facade._build_inputs(request)
            except ports.TeamPlanApprovalPayloadError as exc:
                completion.status = "error"
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={"event_type": "chat.error", "error": str(exc)},
                    is_complete=False,
                )
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=None,
                    is_complete=True,
                )
                return

            # Session-level MCP enable: reconcile to explicit mcp ∪ equipment
            # connectors before the agent runs. Always pass a list (never None):
            # empty clears selection when neither side contributes names.
            params = request.params if isinstance(request.params, dict) else {}
            try:
                runtime_binding = await facade._resolve_runtime_binding_for_request(adapter, request, session_id)
            except (OSError, TypeError, ValueError) as exc:
                completion.status = "error"
                yield AgentResponseChunk(
                    request_id=rid, channel_id=cid,
                    payload={"event_type": "chat.error", "error": str(exc)}, is_complete=True,
                )
                return
            reconcile_kwargs = {
                "model_name": params.get("model_name"),
                "history_before_request_id": request.request_id,
                "execution_request": request,
            }
            if runtime_binding is not None:
                reconcile_kwargs["runtime_binding"] = runtime_binding
            if params.get("model_selection") is not None:
                reconcile_kwargs["model_selection"] = params["model_selection"]
            if "moa_enabled" in params:
                reconcile_kwargs["moa_enabled"] = params["moa_enabled"]
            moa_access = getattr(adapter, "moa_access_kwargs", None)
            if callable(moa_access):
                reconcile_kwargs.update(moa_access(request))
            await facade.reconcile_session_mcp(
                request.session_id,
                ports.compute_chat_send_mcp_needed(params),
                **reconcile_kwargs,
            )
            # Team 的交互回答走普通流式路径，不经过 deliver_control_input。
            # 与单 Agent 使用同一落盘格式，按原问题的 params.request_id 配对。
            await ports.persist_ask_user_answer_history(request, session_id, params)

            # Team 模式：把整个 turn 交给 team_helpers。它先用 turn.text（用户原
            # 文）解析 /debug、$member 与 slash，再用同一个 render() 投递，因此
            # leader 收到的信封与单 agent 逐字段一致。
            if is_team_mode:
                inputs[TEAM_USER_TURN_KEY] = user_turn
                logger.info(
                    "[JiuWenSwarm] Team模式 user turn: text=%s",
                    str(user_turn.text)[:100],
                )

            # cloud memory: before chat hook
            if memory_mode == "cloud":
                inputs["memory_block"] = await facade._get_hook_services().before_memory(
                    request
                )

            from jiuwenswarm.server.runtime.agent_adapter.stream_projection import (
                StreamResponsePipeline,
            )

            pipeline = StreamResponsePipeline(
                request=request,
                adapter=adapter,
                session_id=session_id,
                inputs=inputs,
                memory_mode=memory_mode,
                user_turn=user_turn,
                is_team_mode=is_team_mode,
                ports=ports,
                completion=completion,
                hooks=facade._get_hook_services(),
                queue_capacity=facade.STREAM_QUEUE_MAXSIZE,
                retry_call=facade._make_retry_without_a2ui_call(
                    adapter=adapter, request=request
                ),
            )
            async with aclosing(pipeline.run()) as stream:
                async for chunk in stream:
                    yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            completion.complete("cancelled")
            raise
        except Exception:
            completion.complete("error")
            raise
        finally:
            completion.complete()
