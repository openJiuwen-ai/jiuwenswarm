from __future__ import annotations

import pytest

from jiuwenswarm.common.cron_rpc import (
    CRON_RUN_NOW_MAX_TIMEOUT_SECONDS,
    CRON_RUN_NOW_REVERSE_RPC_METHOD,
    CRON_RUN_NOW_TIMEOUT_SECONDS,
)
from jiuwenswarm.common.reverse_rpc.constants import ERROR_INVALID_REQUEST
from jiuwenswarm.common.reverse_rpc.models import ReverseRpcOrigin, ReverseRpcRoute
from jiuwenswarm.gateway.gateway_push.cron_reverse_rpc import (
    CronRunNowCapability,
    register_cron_reverse_rpc,
)
from jiuwenswarm.gateway.reverse_rpc import (
    CapabilityError,
    CapabilityRegistry,
    ReverseRpcCapabilityContext,
    ReverseRpcDispatcher,
)
from jiuwenswarm.server.reverse_rpc.client import ReverseRpcClient


class FakeController:
    def __init__(
        self,
        *,
        run_id: str = "job-1:1795051274",
        error: Exception | None = None,
    ) -> None:
        self.job_ids: list[str] = []
        self._run_id = run_id
        self._error = error

    async def run_now(self, job_id: str) -> str:
        self.job_ids.append(job_id)
        if self._error is not None:
            raise self._error
        return self._run_id


def _capability_context() -> ReverseRpcCapabilityContext:
    return ReverseRpcCapabilityContext(
        rpc_id="rrpc-1",
        origin=ReverseRpcOrigin(request_id="req-1", channel_id="desktop"),
        route=ReverseRpcRoute(channel_id="desktop"),
        timeout_seconds=CRON_RUN_NOW_TIMEOUT_SECONDS,
        connection_generation=0,
    )


@pytest.mark.asyncio
async def test_capability_returns_run_id() -> None:
    controller = FakeController()
    handler = CronRunNowCapability(controller)

    result = await handler.handle(
        _capability_context(), {"job_id": "job-1"}
    )

    assert result == {"run_id": "job-1:1795051274"}
    assert controller.job_ids == ["job-1"]


@pytest.mark.asyncio
async def test_capability_requires_job_id() -> None:
    handler = CronRunNowCapability(FakeController())

    with pytest.raises(CapabilityError) as exc_info:
        await handler.handle(_capability_context(), {})

    assert exc_info.value.code == ERROR_INVALID_REQUEST


@pytest.mark.asyncio
async def test_capability_controller_unavailable() -> None:
    handler = CronRunNowCapability(None)

    with pytest.raises(CapabilityError) as exc_info:
        await handler.handle(_capability_context(), {"job_id": "job-1"})

    assert exc_info.value.code == "CRON_CONTROLLER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_capability_maps_key_error_to_job_not_found() -> None:
    handler = CronRunNowCapability(
        FakeController(error=KeyError("job not found"))
    )

    with pytest.raises(CapabilityError) as exc_info:
        await handler.handle(_capability_context(), {"job_id": "missing"})

    assert exc_info.value.code == "CRON_JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_capability_maps_trigger_failure() -> None:
    handler = CronRunNowCapability(
        FakeController(error=RuntimeError("scheduler stopped"))
    )

    with pytest.raises(CapabilityError) as exc_info:
        await handler.handle(_capability_context(), {"job_id": "job-1"})

    assert exc_info.value.code == "CRON_TRIGGER_FAILED"


def test_registration_spec() -> None:
    registry = CapabilityRegistry()
    register_cron_reverse_rpc(registry, FakeController())

    spec = registry.resolve(CRON_RUN_NOW_REVERSE_RPC_METHOD)
    assert spec is not None
    assert spec.allow_empty_route is True
    assert spec.supports_cancel is False
    assert spec.cancel_on_disconnect is False
    assert spec.max_timeout_seconds == CRON_RUN_NOW_MAX_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_run_now_client_gateway_capability_loopback() -> None:
    """agent client.call → gateway dispatcher → capability → 响应回填 future。

    全链路回环：请求经 build_request_wire 进 dispatcher，响应经
    LoopbackResponseTransport 完成 pending future——即真实部署里
    gateway→agent 管道连接原路回传 receipt 的等价物。
    """
    registry = CapabilityRegistry()
    controller = FakeController()
    register_cron_reverse_rpc(registry, controller)

    generic_client = ReverseRpcClient()

    class LoopbackResponseTransport:
        async def send(self, response, request):
            del request
            assert generic_client.complete(response)

    dispatcher = ReverseRpcDispatcher(registry, LoopbackResponseTransport())

    class LoopbackRequestTransport:
        async def send(self, wire, route):
            del route
            await dispatcher.handle(wire)

    generic_client.set_transport(LoopbackRequestTransport())

    result = await generic_client.call(
        method=CRON_RUN_NOW_REVERSE_RPC_METHOD,
        payload={"job_id": "job-1"},
        origin=ReverseRpcOrigin(
            request_id="req-1", session_id="sess-1", channel_id="desktop"
        ),
        route=ReverseRpcRoute(channel_id="desktop"),
        timeout=CRON_RUN_NOW_TIMEOUT_SECONDS,
        remote_cancel=False,
    )

    assert result == {"run_id": "job-1:1795051274"}
    assert controller.job_ids == ["job-1"]
