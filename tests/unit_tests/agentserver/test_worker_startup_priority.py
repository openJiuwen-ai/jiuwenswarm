# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Queued cold requests own priority before mailbox readiness can suspend."""

import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.ipc.protocol import KIND_REQUEST, dump_agent_request
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge
from jiuwenswarm.server.worker.preparation import PreparationRegistry
from jiuwenswarm.server.worker.signals import RuntimeSignalSink


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["sdk_entry", "dispatch_failure", "shutdown", "core_failure", "cancel"])
async def test_queued_request_priority_precedes_optional_work_and_is_always_released(outcome):
    holder = ConnHolder()
    dispatcher = WorkerDispatcher(WorkerGatewayBridge(holder))
    preparation = PreparationRegistry()
    mailbox_entered = asyncio.Event()
    mailbox_release = asyncio.Event()
    request_entered = asyncio.Event()
    request_release = asyncio.Event()
    optional_entered = asyncio.Event()
    preparation.register("optional", optional_entered.set)

    class Mailbox:
        async def set_available(self, _available):
            mailbox_entered.set()
            await mailbox_release.wait()

    class Runtime:
        _session_message_service = Mailbox()
        _preparation_registry = preparation
        _startup_preparation_tokens = set()
        _execution_minimum_observed = False

        def attach_gateway_connection(self, _ws, _lock):
            pass

        async def dispatch_parsed_request(self, _ws, _request, _lock):
            request_entered.set()
            if outcome == "dispatch_failure":
                raise RuntimeError("failed before entering SDK")
            await request_release.wait()

    runtime = Runtime()
    await dispatcher.bind_runtime(runtime)
    accepted = AgentRequest(request_id="cold", channel_id="web", session_id="a",
                            req_method=ReqMethod.CHAT_SEND, params={"session_id": "a"})
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "cold-dispatch",
                             "payload": dump_agent_request(accepted)}, None)
    await asyncio.sleep(0)
    assert dispatcher._queued
    signals = RuntimeSignalSink(holder, on_ready=dispatcher.mark_ready)
    signals.runtime_base_ready()
    preparation.mark_minimum_ready()
    try:
        await asyncio.wait_for(mailbox_entered.wait(), 1)
        await asyncio.sleep(0)
        assert not optional_entered.is_set()
        assert preparation._foreground_tokens
        if outcome == "shutdown":
            assert await dispatcher.drain(timeout=1)
        elif outcome == "core_failure":
            await dispatcher.mark_failed("core startup failed")
        elif outcome == "cancel":
            cancel = AgentRequest(request_id="cancel", channel_id="web", session_id="a",
                                  req_method=ReqMethod.CHAT_CANCEL, params={"session_id": "a"})
            assert await dispatcher._drop_queued_cancel(cancel)
        mailbox_release.set()
        if outcome in {"sdk_entry", "dispatch_failure"}:
            await asyncio.wait_for(request_entered.wait(), 1)
            if outcome == "sdk_entry":
                assert not optional_entered.is_set()
                # Mirror the actual SDK observer: all initial request tokens
                # release as soon as model, Rails and context are prepared.
                for token in runtime._startup_preparation_tokens:
                    preparation.resume_background(token)
                runtime._startup_preparation_tokens = set()
                runtime._execution_minimum_observed = True
                request_release.set()
        await asyncio.wait_for(optional_entered.wait(), 1)
        await asyncio.sleep(0)
        assert preparation._foreground_tokens == set()
        assert runtime._startup_preparation_tokens == set()
        assert dispatcher._startup_priorities == {}
    finally:
        mailbox_release.set()
        request_release.set()
        await dispatcher.drain(timeout=1)
        await preparation.close()


@pytest.mark.asyncio
async def test_mailbox_readiness_failure_releases_queued_startup_priority():
    preparation = PreparationRegistry()
    dispatcher = WorkerDispatcher(WorkerGatewayBridge(ConnHolder()))

    class Mailbox:
        async def set_available(self, _available):
            await asyncio.sleep(0)
            raise RuntimeError("mailbox startup failed")

    class Runtime:
        _session_message_service = Mailbox()
        _preparation_registry = preparation
        _startup_preparation_tokens = set()

    runtime = Runtime()
    await dispatcher.bind_runtime(runtime)
    accepted = AgentRequest(request_id="cold", channel_id="web", session_id="a",
                            req_method=ReqMethod.CHAT_SEND, params={"session_id": "a"})
    await dispatcher.handle({"kind": KIND_REQUEST, "dispatch_id": "cold-dispatch",
                             "payload": dump_agent_request(accepted)}, None)
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="mailbox startup failed"):
        await dispatcher.mark_ready()
    assert preparation._foreground_tokens == set()
    assert runtime._startup_preparation_tokens == set()
    assert dispatcher._startup_priorities == {}
    assert await dispatcher.drain(timeout=1)
    await preparation.close()
