"""Cron capability adapter for the generic Reverse RPC dispatcher."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.cron_rpc import (
    CRON_RUN_NOW_MAX_TIMEOUT_SECONDS,
    CRON_RUN_NOW_REVERSE_RPC_METHOD,
)
from jiuwenswarm.common.reverse_rpc.constants import ERROR_INVALID_REQUEST
from jiuwenswarm.gateway.reverse_rpc.errors import CapabilityError
from jiuwenswarm.gateway.reverse_rpc.registry import (
    CapabilityRegistry,
    CapabilitySpec,
    ReverseRpcCapabilityContext,
)

logger = logging.getLogger(__name__)


class CronRunNowCapability:
    """``cron.run_now``：同步触发一次立即执行并把 run_id 随响应回填。

    trigger_run_now 在 gateway 事件循环内同步起跑（毫秒级返回 run_id），不等待
    任务执行完成；回执只含 run_id，执行结果仍走原有 push_update / run records
    通道。这样聊天工具调用终于能拿到真实 run_id（2026-11-19 修复）。

    注意与原 ``_handle_cron_push_payload`` 回执的区别：原回执经
    publish_robot_messages 按业务渠道路由（desktop 会话无对应渠道被静默丢弃），
    Reverse RPC 响应走 gateway→agent 管道连接原路返回，不依赖渠道路由。
    """

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    async def handle(
        self,
        ctx: ReverseRpcCapabilityContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = str((payload or {}).get("job_id") or "").strip()
        if not job_id:
            raise CapabilityError(ERROR_INVALID_REQUEST, "job_id is required")
        if self._controller is None:
            raise CapabilityError(
                "CRON_CONTROLLER_UNAVAILABLE",
                "cron controller is not active in this gateway process",
            )
        try:
            run_id = await self._controller.run_now(job_id)
        except KeyError as exc:
            raise CapabilityError("CRON_JOB_NOT_FOUND", "job not found") from exc
        except Exception as exc:  # noqa: BLE001
            raise CapabilityError("CRON_TRIGGER_FAILED", str(exc)) from exc
        logger.info(
            "[Cron] reverse_rpc run_now job_id=%s run_id=%s rpc_id=%s",
            job_id,
            run_id,
            ctx.rpc_id,
        )
        return {"run_id": str(run_id or "")}


def register_cron_reverse_rpc(registry: CapabilityRegistry, controller: Any) -> None:
    """Register the cron adapter explicitly at Gateway composition time."""

    registry.register(
        CapabilitySpec(
            method=CRON_RUN_NOW_REVERSE_RPC_METHOD,
            handler=CronRunNowCapability(controller),
            # trigger 是毫秒级同步操作，无中途取消语义；断连时宁可让触发完成。
            supports_cancel=False,
            cancel_on_disconnect=False,
            max_timeout_seconds=CRON_RUN_NOW_MAX_TIMEOUT_SECONDS,
            # cron 能力不使用 route（单 gateway 连接定向），允许空路由。
            allow_empty_route=True,
        )
    )
