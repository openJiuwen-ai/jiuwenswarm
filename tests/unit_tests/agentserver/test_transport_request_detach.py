# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A closed transport retires its waiter, never another request or producer."""
from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.supervisor.worker_registry import WorkerSlot
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge


class RecordingConnection:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class HoldingRuntime:
    def __init__(self):
        self.active = set()
        self.methods = []
        self.producer = asyncio.create_task(asyncio.Event().wait())

    async def dispatch_parsed_request(self, bridge, request, lock):
        self.methods.append(request.req_method)
        marker = request.params['marker']
        self.active.add(marker)
        try:
            await asyncio.Event().wait()
        finally:
            self.active.remove(marker)


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


def request(marker):
    return AgentRequest(request_id='reused', channel_id='web', session_id='same-team',
        req_method=ReqMethod.CHAT_SEND, params={'marker': marker, 'mode': 'team', 'attach_only': True}, is_stream=True)


def worker():
    conn = RecordingConnection()
    holder = ConnHolder()
    holder.open_session(conn)
    return WorkerDispatcher(WorkerGatewayBridge(holder)), conn, holder


@pytest.mark.asyncio
async def test_front_cancellation_retires_only_its_worker_stream():
    dispatcher, conn, holder = worker()
    runtime = HoldingRuntime()
    await dispatcher.bind_runtime(runtime)
    supervisor = RuntimeSupervisor(Readiness())
    worker_id = supervisor._affinity.worker_for('same-team')
    supervisor._registry.upsert(WorkerSlot(worker_id=worker_id))
    supervisor._peer_generation = holder.generation

    class Forward:
        async def send(self, message):
            await dispatcher.handle(message, conn)

    supervisor._conn = Forward()
    ws, send_lock = object(), asyncio.Lock()
    old = asyncio.create_task(supervisor.dispatch_parsed_request(ws, request('old'), send_lock))
    new = None
    try:
        await until(lambda: runtime.active == {'old'})
        new = asyncio.create_task(supervisor.dispatch_parsed_request(ws, request('new'), send_lock))
        await until(lambda: runtime.active == {'old', 'new'})
        new_future = supervisor._pending['reused']
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
        assert runtime.active == {'new'}, 'Worker retained an orphan transport waiter'
        assert supervisor._pending['reused'] is new_future
        assert supervisor._routes['reused'] == (ws, send_lock)
        assert not new_future.done()
        assert not runtime.producer.done()
        assert runtime.methods == [ReqMethod.CHAT_SEND, ReqMethod.CHAT_SEND]
    finally:
        for task in (old, new):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (old, new) if task is not None), return_exceptions=True)
        await dispatcher.drain(timeout=1)
        runtime.producer.cancel()
        await asyncio.gather(runtime.producer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('queued', [False, True])
async def test_detach_fences_generation_and_request_token(queued):
    from jiuwenswarm.server.ipc.protocol import dump_agent_request
    dispatcher, conn, holder = worker()
    runtime = HoldingRuntime()
    if not queued:
        await dispatcher.bind_runtime(runtime)
    try:
        for token in ('old-token', 'new-token'):
            await dispatcher.handle({'kind': 'request', 'generation': holder.generation,
                'request_token': token, 'payload': dump_agent_request(request(token))}, conn)
        await until(lambda: len(dispatcher._queued) == 2 if queued else len(runtime.active) == 2)
        await dispatcher.handle({'kind': 'request.detach', 'generation': holder.generation - 1,
            'request_token': 'old-token'}, conn)
        await dispatcher.handle({'kind': 'request.detach', 'generation': holder.generation,
            'request_token': ''}, conn)
        assert len(dispatcher._queued if queued else runtime.active) == 2
        await dispatcher.handle({'kind': 'request.detach', 'generation': holder.generation,
            'request_token': 'old-token'}, conn)
        if queued:
            assert len(dispatcher._queued) == 1
            await dispatcher.bind_runtime(runtime)
            await until(lambda: runtime.active == {'new-token'})
        assert runtime.active == {'new-token'}
        assert not runtime.producer.done()
    finally:
        await dispatcher.drain(timeout=1)
        runtime.producer.cancel()
        await asyncio.gather(runtime.producer, return_exceptions=True)


@pytest.mark.asyncio
async def test_detach_during_runtime_binding_never_starts_the_old_request():
    from jiuwenswarm.server.ipc.protocol import dump_agent_request
    dispatcher, conn, holder = worker()
    attaching, release = asyncio.Event(), asyncio.Event()

    class BindingRuntime(HoldingRuntime):
        async def attach_gateway_connection(self, bridge, lock):
            attaching.set()
            await release.wait()

    runtime = BindingRuntime()
    binding = None
    dispatcher._attached = True
    try:
        await dispatcher.handle({'kind': 'request', 'generation': holder.generation,
            'request_token': 'old-token', 'payload': dump_agent_request(request('old'))}, conn)
        await until(lambda: len(dispatcher._queued) == 1)
        binding = asyncio.create_task(dispatcher.bind_runtime(runtime))
        await attaching.wait()
        await dispatcher.handle({'kind': 'request.detach', 'generation': holder.generation,
            'request_token': 'old-token'}, conn)
        release.set()
        await binding
        await asyncio.sleep(0)
        assert not runtime.active
        assert not runtime.methods
    finally:
        release.set()
        if binding is not None:
            await asyncio.gather(binding, return_exceptions=True)
        await dispatcher.drain(timeout=1)
        runtime.producer.cancel()
        await asyncio.gather(runtime.producer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('stream,observer', [(False, True), (True, False)])
async def test_disconnect_does_not_cancel_an_accepted_turn_or_mutation(stream, observer):
    dispatcher, conn, holder = worker()
    runtime = HoldingRuntime()
    await dispatcher.bind_runtime(runtime)
    supervisor = RuntimeSupervisor(Readiness())
    supervisor._registry.upsert(WorkerSlot(worker_id=supervisor._affinity.worker_for('same-team')))
    supervisor._peer_generation = holder.generation

    class Forward:
        async def send(self, message):
            await dispatcher.handle(message, conn)

    supervisor._conn = Forward()
    accepted = request('accepted')
    accepted.is_stream = stream
    if not stream:
        accepted.req_method = ReqMethod.SESSION_DELETE
    accepted.params['attach_only'] = observer
    task = asyncio.create_task(supervisor.dispatch_parsed_request(object(), accepted, asyncio.Lock()))
    try:
        await until(lambda: runtime.active == {'accepted'})
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert runtime.active == {'accepted'}
        assert not runtime.producer.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await dispatcher.drain(timeout=1)
        runtime.producer.cancel()
        await asyncio.gather(runtime.producer, return_exceptions=True)


@pytest.mark.asyncio
async def test_finished_front_forwarding_releases_its_passive_worker_waiter():
    dispatcher, conn, holder = worker()
    runtime = HoldingRuntime()
    await dispatcher.bind_runtime(runtime)
    supervisor = RuntimeSupervisor(Readiness())
    supervisor._registry.upsert(WorkerSlot(worker_id=supervisor._affinity.worker_for('same-team')))
    supervisor._peer_generation = holder.generation

    class Forward:
        async def send(self, message):
            await dispatcher.handle(message, conn)

    supervisor._conn = Forward()
    task = asyncio.create_task(supervisor.dispatch_parsed_request(object(), request('observer'), asyncio.Lock()))
    try:
        await until(lambda: runtime.active == {'observer'})
        # A forwarding failure can finish Front normally instead of cancelling
        # its task. The Worker still owns a persistent observation stream.
        supervisor._pending['reused'].set_result({'ok': False, 'delivered': True})
        await task
        assert not runtime.active
        assert not runtime.producer.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await dispatcher.drain(timeout=1)
        runtime.producer.cancel()
        await asyncio.gather(runtime.producer, return_exceptions=True)
