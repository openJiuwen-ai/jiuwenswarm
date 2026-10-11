# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""连接刚接上时的三件事。

初始化连接：对方先报自己能做什么，服务器回自己能做什么。
创建会话：新开一段对话。
从旧会话分出新会话：照着一段已经有的对话，另开一段新的。

ACP 是编辑器走的那条连接。它报上来的能力按这条连接记住，
后面的请求还能用到。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.runtime.session_provisioner import (
    SessionCreateInput,
    SessionForkInput,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
)
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


async def handle_initialize(ctx: RequestContext) -> None:
    """处理初始化连接。

    让 Agent 管理器做完初始化，再把服务器自己的能力回给对方。
    """
    request = ctx.request
    services = ctx.services
    from jiuwenswarm.server.runtime.agent_manager import ACP_DEFAULT_CAPABILITIES

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
            services.set_acp_client_capabilities(ctx.connection_id, client_capabilities)

        channel_id = request.channel_id or "default"
        capabilities = await services.agent_manager.initialize(
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
        await ctx.sink.send_response(resp)

        logger.info("[AgentServer] initialize completed: capabilities=%s", capabilities)

    except Exception as e:
        logger.exception("[AgentServer] initialize failed: %s", e)  # noqa: TRY401
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
        await ctx.sink.send_response(resp)


async def handle_session_create(ctx: RequestContext) -> None:
    """Translate ``session.create`` between WebSocket wire and Runtime."""
    request = ctx.request
    services = ctx.services
    operation = "session.create"
    logger.info(
        "[AgentServer] %s: request_id=%s",
        operation,
        request.request_id,
    )

    runtime = None
    prepared = None
    response_delivered = False

    async def abort_prepared(
        primary_error: BaseException | None,
    ) -> None:
        if (
            runtime is None
            or prepared is None
            or prepared.state is not SessionProvisionState.PREPARED
        ):
            return
        try:
            await runtime.abort_session_provision(prepared)
        except asyncio.CancelledError:
            if primary_error is None:
                raise
            logger.warning(
                "[AgentServer] session.create abort was cancelled while "
                "preserving %s",
                type(primary_error).__name__,
            )
        except Exception as abort_error:  # noqa: BLE001
            logger.warning(
                "[AgentServer] session.create abort failed while preserving %s: %s",
                (
                    type(primary_error).__name__
                    if primary_error is not None
                    else "normal completion"
                ),
                abort_error,
            )

    try:
        channel_id = request.channel_id or "default"
        params = request.params if isinstance(request.params, dict) else {}
        persist_session_supplied = "persist_session" in params
        raw_persist_session = params.get("persist_session", False)
        if not isinstance(raw_persist_session, bool):
            raise SessionProvisionError(
                "persist_session must be a boolean",
                code="BAD_REQUEST",
            )

        explicit_work_mode_marker = params.get("_work_mode_explicit")
        requested = params.get("session_id")
        requested_session_id = (
            requested.strip() if isinstance(requested, str) else ""
        )
        if requested_session_id and str(channel_id).strip().lower() == "tui":
            logger.warning(
                "[AgentServer] TUI supplied session_id via session.create; "
                "bypassing prewarm compatibility path: session_id=%s",
                requested_session_id,
            )

        runtime = services.execution_runtime()
        await runtime.start()
        prepared = await runtime.prepare_session_create(
            SessionCreateInput(
                channel_id=channel_id,
                requested_session_id=requested_session_id or None,
                previous_session_id=str(
                    params.get("previous_session_id") or ""
                ).strip(),
                create_token=str(params.get("create_token") or "").strip(),
                persist_session=raw_persist_session,
                persist_session_supplied=persist_session_supplied,
                mode=params.get("mode", "agent"),
                previous_mode=params.get("previous_mode"),
                is_swarm=bool(params.get("is_swarm")),
                team_hint=bool(params.get("team")),
                project_id=params.get("project_id", ""),
                project_dir=params.get("project_dir", ""),
                cwd=params.get("cwd", ""),
                work_mode=params.get("work_mode"),
                work_mode_explicit=(
                    explicit_work_mode_marker
                    if isinstance(explicit_work_mode_marker, bool)
                    else None
                ),
                title=params.get("title", ""),
                user_id=str(
                    getattr(request, "user_id", "")
                    or params.get("user_id", "")
                    or ""
                ).strip(),
                model_name=str(params.get("model_name") or "").strip(),
                cron_id=str(params.get("cron_id") or "").strip(),
            )
        )
        params.pop("_work_mode_explicit", None)
        result = prepared.result

        # Preserve the established mutation visible to in-process callers.
        params["project_id"] = result.project_id
        params["project_dir"] = result.project_dir
        params["work_mode"] = result.work_mode
        params["mode"] = result.canonical_mode

        payload = {
            "sessionId": result.session_id,
            "session_id": result.session_id,
            "projectId": result.project_id,
            "projectDir": result.project_dir,
            "workMode": result.work_mode,
            "persist_session": result.persist_session,
            "prewarm_hit": result.prewarm_hit,
            "prewarm_status": result.prewarm_status,
        }
        if result.explicit_id_compatibility:
            payload.update(
                {
                    "created": result.created,
                    "mode": result.canonical_mode,
                }
            )

        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
        await ctx.sink.send_response(response)
        response_delivered = True

        await runtime.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
            context=SessionProvisionCommitContext(
                foreground_scope_id=str(params.get("view_id") or f"ws:{ctx.connection_id}")
            ),
        )
        logger.info(
            "[AgentServer] %s completed: session_id=%s",
            operation,
            result.session_id,
        )
    except SessionProvisionError as error:
        logger.warning("[AgentServer] %s rejected: %s", operation, error)
        await abort_prepared(error)
        if response_delivered:
            return
        payload = {"error": str(error)}
        if error.code is not None:
            payload["code"] = error.code
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload=payload,
        )
        await ctx.sink.send_response(response)
    except Exception as error:
        logger.exception("[AgentServer] %s failed: %s", operation, error)  # noqa: TRY401
        await abort_prepared(error)
        if response_delivered:
            return
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(error)},
        )
        await ctx.sink.send_response(response)
    finally:
        await abort_prepared(sys.exception())


async def handle_session_fork(ctx: RequestContext) -> None:
    """从旧会话分出一段新会话，再把新会话编号回给对方。"""
    request = ctx.request
    services = ctx.services
    logger.info(
        "[AgentServer] session.fork: request_id=%s", request.request_id
    )

    runtime = None
    prepared = None
    try:
        params = request.params if isinstance(request.params, dict) else {}
        if "side_conversation" in params:
            raise SessionProvisionError(
                "side_conversation is no longer supported",
                code="BAD_REQUEST",
            )
        source = str(params.get("source_session_id") or "").strip()
        target = str(params.get("target_session_id") or "").strip()
        fork_title = str(params.get("title") or "").strip()
        equipment_override = params.get("session_equipment_override")
        if equipment_override is not None and not isinstance(equipment_override, dict):
            raise SessionProvisionError(
                "session_equipment_override must be an object",
                code="BAD_REQUEST",
            )
        fork_point = params.get("fork_point")
        if not isinstance(fork_point, dict):
            fork_point = {}
        channel_id = request.channel_id or "default"

        if not source:
            raise SessionProvisionError(
                "source_session_id is required",
                code="BAD_REQUEST",
            )

        runtime = services.execution_runtime()
        await runtime.start()
        prepared = await runtime.prepare_session_fork(
            SessionForkInput(
                channel_id=channel_id,
                source_session_id=source,
                target_session_id=target or None,
                title=fork_title,
                cutoff_message_id=str(
                    fork_point.get("message_id") or ""
                ).strip(),
                cutoff_role=str(fork_point.get("role") or "").strip(),
                cutoff_content=str(fork_point.get("content") or ""),
                cutoff_timestamp=fork_point.get("timestamp"),
                session_equipment_override=equipment_override,
            )
        )
        result = await runtime.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "session_id": result.session_id,
                "source_session_id": result.source_session_id,
                "title": result.title,
            },
        )
        await ctx.sink.send_response(resp)

        logger.info(
            "[AgentServer] session.fork completed: source=%s target=%s title=%s",
            source,
            result.session_id,
            result.title,
        )

    except SessionProvisionError as e:
        logger.warning("[AgentServer] session.fork rejected: %s", e)
        payload = {"error": str(e)}
        if e.code is not None:
            payload["code"] = e.code
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload=payload,
        )
        await ctx.sink.send_response(resp)
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
        await ctx.sink.send_response(resp)
    except Exception as e:
        logger.exception("[AgentServer] session.fork failed: %s", e)  # noqa: TRY401
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
        await ctx.sink.send_response(resp)
    finally:
        if (
            runtime is not None
            and prepared is not None
            and prepared.state is SessionProvisionState.PREPARED
        ):
            primary_error = sys.exception()
            try:
                await runtime.abort_session_provision(prepared)
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
                logger.warning(
                    "[AgentServer] session.fork abort was cancelled while "
                    "preserving %s",
                    type(primary_error).__name__,
                )
            except Exception as abort_exc:  # noqa: BLE001
                logger.warning(
                    "[AgentServer] session.fork abort failed: %s",
                    abort_exc,
                )
