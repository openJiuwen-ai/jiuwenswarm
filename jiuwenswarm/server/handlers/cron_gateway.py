# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway → AgentServer 的 cron 回执（HTTP / WebSocket 共用逻辑）。

企业版 runtime 模式没有长连接 WebSocket：Gateway 用 HTTP 把
``cron.command.ack`` / ``cron.run.now.ack`` / ``cron.jobs.sync`` 打回来。
这里唤醒工具侧等待的 Future，或装入任务快照。
"""

from __future__ import annotations

import logging

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


async def handle_cron_gateway_callback(ctx: RequestContext) -> None:
    """处理 Gateway 的 cron 回执 / 快照同步。"""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    method = request.req_method

    from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
        install_gateway_jobs_snapshot,
        resolve_gateway_cron_command_ack,
        resolve_gateway_run_ack,
    )

    try:
        if method == ReqMethod.CRON_JOBS_SYNC:
            install_gateway_jobs_snapshot(
                params.get("jobs", []),
                user_id=request.user_id,
            )
        elif method == ReqMethod.CRON_COMMAND_ACK:
            resolve_gateway_cron_command_ack(
                str(params.get("command_id") or ""),
                {"data": params.get("data")},
            )
        elif method == ReqMethod.CRON_RUN_NOW_ACK:
            resolve_gateway_run_ack(
                str(params.get("ack_request_id") or ""),
                str(params.get("run_id") or ""),
            )
        else:
            await ctx.sink.send_response(
                AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": f"unsupported cron gateway method: {method}"},
                    metadata=request.metadata,
                )
            )
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CronGateway] callback failed method=%s request_id=%s: %s",
            method,
            request.request_id,
            exc,
            exc_info=True,
        )
        await ctx.sink.send_response(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        )
        return

    await ctx.sink.send_response(
        AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"status": "ok"},
            metadata=request.metadata,
        )
    )
