# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""连接引导/会话域 handler"""

from __future__ import annotations

import asyncio
import logging

from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import (
    _background_session_kvc_tasks,
    _log_background_session_kvc_failure,
    _sessions_dir_for_request,
    bootstrap_preconditions,
    resolve_agent_request_mode,
)
from jiuwenswarm.server.runtime.agent_manager import ACP_DEFAULT_CAPABILITIES

logger = logging.getLogger(__name__)


async def handle_initialize(ctx: RequestContext) -> None:
    """处理 initialize 方法（非流式）.

    调用 AgentManager.initialize 完成初始化，返回 capabilities。

    Args:
        ctx: 请求上下文（``ctx.request`` 为 AgentRequest，``ctx.sink`` 为响应出口）。
    """
    # 仅桥接 request，其余保持与默认路径一致。
    async with bootstrap_preconditions(ctx.request):
        request = ctx.request
        logger.info("[AgentServer] initialize: request_id=%s channel_id=%s", request.request_id, request.channel_id)

        try:
            params = request.params if isinstance(request.params, dict) else {}
            client_capabilities = params.get("clientCapabilities", {})
            logger.info(
                "[AgentServer] initialize clientCapabilities: %s",
                client_capabilities,
            )

            extra_config = {
                "protocol_version": params.get("protocolVersion", "0.1.0"),
                "client_capabilities": client_capabilities,
            }
            if request.channel_id == "acp":
                ctx.services.set_acp_client_capabilities(ctx.connection_id, client_capabilities)

            channel_id = request.channel_id or "default"
            capabilities = await ctx.services.agent_manager.initialize(
                channel_id=channel_id,
                extra_config=extra_config,
            )
            if capabilities is None:
                capabilities = ACP_DEFAULT_CAPABILITIES.copy()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=capabilities,
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            await ctx.sink.send_wire(wire)

            logger.info("[AgentServer] initialize completed: capabilities=%s", capabilities)

        except Exception as e:
            logger.exception("[AgentServer] initialize failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            await ctx.sink.send_wire(wire)


async def handle_session_create(ctx: RequestContext) -> None:
    """处理 session.create 方法.

    调用 AgentManager.create_session 创建会话，返回 session_id。
    同时将 project_dir/project_id 等字段写入会话元数据(metadata.json)并落盘。
    project_id / project_dir 绑定规则(详见
    project_store.resolve_session_project_binding):两者皆空→默认项目;
    仅传 project_id→自动补齐 path;同时传→校验一致性;仅传 path→拒绝。

    Args:
        ws: WebSocket 连接
        request: AgentRequest
        send_lock: 发送锁
    """
    # 仅桥接 request，其余保持与默认路径一致。
    async with bootstrap_preconditions(ctx.request):
        request = ctx.request
        logger.info("[AgentServer] session.create: request_id=%s", request.request_id)

        try:
            channel_id = request.channel_id or "default"
            params = request.params if isinstance(request.params, dict) else {}
            mode, _, canonical_mode = resolve_agent_request_mode(params.get("mode", "agent"))
            explicit_session_id = params.get("session_id")
            previous_session_id = str(params.get("previous_session_id") or "").strip()
            if isinstance(explicit_session_id, str) and explicit_session_id.strip():
                raise ValueError(
                    "session.create no longer accepts session_id; use session.switch to restore"
                )
            # Step 1: 归一化 work_mode / project_id / project_dir 三元组
            # (与 web _session_create 共用同一 helper，保持主路径/fallback 一致)
            from jiuwenswarm.server.runtime.session.work_mode import resolve_session_work_mode_params
            binding = resolve_session_work_mode_params(params, channel_id=channel_id)
            if binding.error:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": binding.error, "code": binding.code},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                await ctx.sink.send_wire(wire)
                return

            # 校验并解析 project_id / project_dir 绑定关系:
            # 一致性校验、按 project_id 自动补齐 project_dir、禁止单传 project_dir
            from jiuwenswarm.server.runtime.session import project_store
            from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE, is_default_project_id
            project_id, project_dir, p_err, p_code = project_store.resolve_session_project_binding(
                binding.project_id, binding.project_dir
            )
            if p_err:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": p_err, "code": p_code},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                await ctx.sink.send_wire(wire)
                return

            # Step 3: 确定最终 work_mode
            # 对真实 project_id: 最终 work_mode 以 Project 记录为准;若请求显式传了
            # work_mode 且与 Project 不一致 → BAD_REQUEST(设计文档 §4.1.6)
            # 对默认项目: 使用 binding 归一化的 work_mode
            #
            # has_explicit_work_mode 判定逻辑:
            # - gateway 路径: params 含 _work_mode_explicit marker(由 gateway 注入),
            #   消费后立即 pop。marker=True 表示用户显式传了 work_mode(需一致性校验);
            #   marker=False 表示 gateway 注入的通道默认值(跳过校验)。
            # - 直连路径(非 gateway): marker 缺失,使用 binding.has_explicit_work_mode
            #   (此时 params 为原始值,binding 计算结果正确)。
            explicit_work_mode_marker = params.pop("_work_mode_explicit", None)
            if isinstance(explicit_work_mode_marker, bool):
                has_explicit_work_mode = explicit_work_mode_marker
            else:
                # marker 缺失:直连 AgentServer 调用方,params 为原始值,
                # binding.has_explicit_work_mode 正确反映用户是否显式传了 work_mode
                has_explicit_work_mode = binding.has_explicit_work_mode
            if not is_default_project_id(project_id):
                proj = project_store.get_project_by_id(project_id, cache_bust=True)
                if proj is not None:
                    project_work_mode = proj.work_mode or DEFAULT_WEB_WORK_MODE
                    if has_explicit_work_mode and project_work_mode != binding.work_mode:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={
                                "error": f"work_mode mismatch: project is '{project_work_mode}' \
                                    but request specified '{binding.work_mode}'",
                                "code": "BAD_REQUEST",
                            },
                        )
                        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                        await ctx.sink.send_wire(wire)
                        return
                    final_work_mode = project_work_mode
                else:
                    # 竞态: project 已被其他进程删除/隐藏。
                    # 不创建指向不存在项目的会话,返回 NOT_FOUND 由调用方决定回退策略。
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={
                            "error": f"project not found: {project_id}",
                            "code": "NOT_FOUND",
                        },
                    )
                    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                    await ctx.sink.send_wire(wire)
                    return
            else:
                final_work_mode = binding.work_mode

            # 将解析后的字段回写 params,保持与 fallback 路径(app_web_handlers)一致,
            # 后续若读取 params.project_id/project_dir/work_mode 可直接拿到规范化值
            params["project_id"] = project_id
            params["project_dir"] = project_dir
            params["work_mode"] = final_work_mode

            is_swarm = bool(params.get("is_swarm")) or canonical_mode in {
                "team",
                "team.plan",
                "code.team",
            }
            if not is_swarm:
                mode, _, canonical_mode = resolve_agent_request_mode(
                    canonical_mode,
                    work_mode=final_work_mode,
                )
                params["mode"] = canonical_mode
            prewarm_eligible = (
                not is_swarm
                and canonical_mode in {"agent", "code", "code.normal"}
            )
            create_token = str(params.get("create_token") or "").strip()
            if not create_token:
                raise ValueError("create_token is required")
            claim = await ctx.services.agent_manager.claim_prewarmed_session(
                channel_id=channel_id,
                project_id=project_id,
                project_dir=project_dir,
                work_mode=final_work_mode,
                is_swarm=is_swarm,
                prewarm_eligible=prewarm_eligible,
                create_token=create_token,
            )
            session_id = claim.session_id

            # 会话目录已存在则拒绝,避免覆盖既有会话元数据(与 web 本地 handler 一致)
            sessions_root = _sessions_dir_for_request(request)
            from jiuwenswarm.server.runtime.session.session_metadata import (
                resolve_session_subdir,
            )
            session_dir = resolve_session_subdir(
                session_id,
                sessions_root=sessions_root,
            )
            legacy_session_dir = resolve_session_subdir(
                session_id,
                sessions_root=get_agent_sessions_dir(),
            )
            if session_dir is None or legacy_session_dir is None:
                raise ValueError("invalid session_id")
            legacy_meta = legacy_session_dir / "metadata.json"
            if (session_dir / "metadata.json").is_file() or legacy_meta.is_file():
                ctx.services.agent_manager.activate_session_prewarm(session_id)
                if create_token:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "sessionId": session_id,
                            "session_id": session_id,
                            "projectId": project_id,
                            "projectDir": project_dir,
                            "workMode": final_work_mode,
                            "prewarm_hit": claim.prewarm_hit,
                            "prewarm_status": claim.prewarm_status,
                        },
                    )
                    wire = encode_agent_response_for_wire(
                        resp, response_id=request.request_id
                    )
                    await ctx.sink.send_wire(wire)
                    return
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session already exists", "code": "ALREADY_EXISTS"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                await ctx.sink.send_wire(wire)
                return

            # 初始化会话元数据(同步写盘),将 project_dir/project_id 等字段落盘
            from jiuwenswarm.server.runtime.session.session_metadata import (
                init_session_metadata,
            )
            init_session_metadata(
                session_id=session_id,
                channel_id=channel_id,
                user_id=params.get("user_id", ""),
                title=params.get("title", ""),
                mode=canonical_mode,
                project_dir=project_dir,
                project_id=project_id,
                work_mode=final_work_mode,
                cron_id=str(params.get("cron_id") or "").strip(),
                sessions_root=_sessions_dir_for_request(request, params=params),
            )
            ctx.services.agent_manager.activate_session_prewarm(session_id)

            # team prepare 必须在 ack 前完成，避免首条 chat.send 与分布式切换竞态；
            # 可选 KVC 信号放到回包后异步，避免拖慢 create RPC。
            lifecycle_params = dict(params)
            lifecycle_params["mode"] = canonical_mode
            (
                _target_is_team,
                _resolved_mode,
                switch_context,
                team_manager,
                dispatch_signals,
            ) = await ctx.services.prepare_session_switch_owner(
                channel_id=channel_id,
                target_session_id=session_id,
                previous_session_id=previous_session_id,
                params=lifecycle_params,
                reason="session.create switch: ",
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "sessionId": session_id,
                    "session_id": session_id,
                    "projectId": project_id,
                    "projectDir": project_dir,
                    "workMode": final_work_mode,
                    "prewarm_hit": claim.prewarm_hit,
                    "prewarm_status": claim.prewarm_status,
                },
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            await ctx.sink.send_wire(wire)

            logger.info("[AgentServer] session.create completed: session_id=%s", session_id)

            if switch_context is not None and dispatch_signals is not None:
                kvc_task = asyncio.create_task(
                    ctx.services.dispatch_session_switch_kvc(
                        channel_id=channel_id,
                        target_session_id=session_id,
                        previous_session_id=previous_session_id,
                        reason="session.create switch: ",
                        context=switch_context,
                        team_manager=team_manager,
                        dispatch_signals=dispatch_signals,
                    ),
                    name=f"session-create-kvc-{session_id}",
                )
                _background_session_kvc_tasks.add(kvc_task)
                kvc_task.add_done_callback(_background_session_kvc_tasks.discard)
                kvc_task.add_done_callback(_log_background_session_kvc_failure)

        except Exception as e:
            logger.exception("[AgentServer] session.create failed: %s", e)
            await ctx.services.agent_manager.release_session_prewarm_claim(
                locals().get("session_id")
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            await ctx.sink.send_wire(wire)


async def handle_session_fork(ctx: RequestContext) -> None:
    """Handle session.fork: filesystem copy + in-memory context copy.

    Args:
        ctx: Request context. ``ctx.request`` carries source_session_id,
            target_session_id and title; ``ctx.sink`` is the response outlet.
    """
    # 仅桥接 request，其余保持与默认路径一致。
    async with bootstrap_preconditions(ctx.request):
        request = ctx.request
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            copy_session_context,
            copy_session_state,
            fork_session,
        )

        logger.info(
            "[AgentServer] session.fork: request_id=%s", request.request_id
        )

        try:
            params = request.params if isinstance(request.params, dict) else {}
            source = str(params.get("source_session_id") or "").strip()
            target = str(params.get("target_session_id") or "").strip()
            fork_title = str(params.get("title") or "").strip()
            channel_id = request.channel_id or "default"

            if not source:
                raise ValueError("source_session_id is required")
            if not target:
                target = await ctx.services.agent_manager.create_session(channel_id=channel_id)

            # 1. Filesystem fork (copies history.json, writes metadata)
            result = fork_session(
                source_session_id=source,
                target_session_id=target,
                title=fork_title,
                channel_id=channel_id,
            )

            # 2. Copy in-memory context (LLM conversation history)
            agent = ctx.services.agent_manager.get_agent_nowait(channel_id)
            deep_agent = None
            if agent is not None:
                deep_agent = await agent.ensure_instance()
                await copy_session_context(deep_agent, source, target)
            else:
                logger.warning(
                    "[AgentServer] session.fork: no agent for channel %s, "
                    "in-memory context copy skipped",
                    channel_id,
                )

            # 3. Copy DeepAgentState (task_plan, plan_mode, etc.)
            from openjiuwen.core.single_agent.schema.agent_card import AgentCard

            await copy_session_state(
                source_session_id=source,
                target_session_id=target,
                card=deep_agent.card if deep_agent is not None else AgentCard(id="jiuwenswarm", name="jiuwenswarm"),
                deep_agent=deep_agent,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result,
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            await ctx.sink.send_wire(wire)

            logger.info(
                "[AgentServer] session.fork completed: source=%s target=%s title=%s",
                source, target, result.get("title", ""),
            )

        except ValueError as e:
            logger.warning("[AgentServer] session.fork ValueError: %s", e)
            code = (
                "NOT_FOUND" if "not found" in str(e)
                else "ALREADY_EXISTS" if "already exists" in str(e)
                else "BAD_REQUEST"
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e), "code": code},
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            await ctx.sink.send_wire(wire)
        except Exception as e:
            logger.exception("[AgentServer] session.fork failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            await ctx.sink.send_wire(wire)


async def handle_acp_tool_response(ctx: RequestContext) -> None:
    # 仅桥接 request，其余保持与默认路径一致。
    async with bootstrap_preconditions(ctx.request):
        request = ctx.request
        params = request.params if isinstance(request.params, dict) else {}
        jsonrpc_id = params.get("jsonrpc_id")
        response_payload = params.get("response")
        if not isinstance(response_payload, dict):
            response_payload = {}

        if get_acp_output_manager().complete_jsonrpc_response(jsonrpc_id, response_payload):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"accepted": True},
            )
        else:
            logger.info(
                "[AgentServer] ignore unknown/late acp tool response: jsonrpc_id=%s request_id=%s",
                jsonrpc_id,
                request.request_id,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "accepted": False,
                    "ignored": True,
                    "reason": "unknown_or_late_response",
                    "jsonrpc_id": jsonrpc_id,
                },
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
