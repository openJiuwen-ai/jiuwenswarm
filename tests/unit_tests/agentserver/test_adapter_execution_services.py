# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ownership and rejection contracts of the V2 adapter composition services."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.execution_driver import ExecutionDriver
from jiuwenswarm.server.runtime.agent_adapter.model_factory import ModelFactory
from jiuwenswarm.server.runtime.agent_adapter.session_assembler import SessionAssembler


@pytest.mark.asyncio
async def test_full_handoff_queue_cannot_block_producer_cleanup():
    blocked, closed = asyncio.Event(), asyncio.Event()

    async def produce():
        try:
            yield "first"
            blocked.set()
            yield "second"
        finally:
            closed.set()

    driver = ExecutionDriver(produce, capacity=1)
    driver.start()
    await asyncio.wait_for(blocked.wait(), 1)
    assert driver.queue.qsize() == 1
    await asyncio.wait_for(driver.close(), 1)
    assert closed.is_set() and driver.done.is_set()
    assert driver.queue.get_nowait() == ("chunk", "first")
    with pytest.raises(asyncio.CancelledError):
        driver.raise_if_cancelled()


@pytest.mark.asyncio
async def test_producer_self_cancellation_drains_output_before_propagating():
    async def produce():
        yield "accepted output"
        raise asyncio.CancelledError("SDK output owner cancelled")

    driver = ExecutionDriver(produce, capacity=1)
    driver.start()
    await asyncio.wait_for(driver.done.wait(), 1)
    assert driver.queue.get_nowait() == ("chunk", "accepted output")
    with pytest.raises(asyncio.CancelledError, match="SDK output owner"):
        driver.raise_if_cancelled()
    await driver.close()


@pytest.mark.asyncio
async def test_repeated_consumer_cancellation_cannot_abandon_sdk_lease_cleanup():
    closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def produce():
        try:
            yield "first"
            yield "blocked on full queue"
        finally:
            closing.set()
            await release.wait()
            closed.set()

    driver = ExecutionDriver(produce, capacity=1)
    driver.start()
    await asyncio.sleep(0)
    operation = asyncio.create_task(driver.close())
    await asyncio.wait_for(closing.wait(), 1)
    operation.cancel("disconnect")
    await asyncio.sleep(0)
    operation.cancel("shutdown")
    await asyncio.sleep(0)
    assert not operation.done() and not closed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 1)
    assert closed.is_set() and driver.done.is_set()


@pytest.mark.asyncio
async def test_failed_producer_keeps_prior_output_and_structured_exception():
    error = ValueError("provider failed")

    async def produce():
        yield "accepted output"
        raise error

    driver = ExecutionDriver(produce, capacity=2)
    driver.start()
    await asyncio.wait_for(driver.done.wait(), 1)
    assert driver.queue.get_nowait() == ("chunk", "accepted output")
    assert driver.queue.get_nowait() == ("error", error)
    driver.raise_if_cancelled()
    await driver.close()


def test_failed_model_recipe_is_retryable_and_publishes_alias_only_on_success():
    attempts, cache = [], {}
    model = object()

    def build(client, _config, **_kwargs):
        attempts.append(client["model_name"])
        if len(attempts) == 1:
            raise ValueError("credential provider temporarily unavailable")
        return model

    args = dict(
        recipes={"selected": {"model_client_config": {"model_name": "chosen"}}},
        aliases={"friendly": "selected"},
        cache=cache,
        build=build,
    )
    assert ModelFactory.materialize("selected", **args) is None
    assert cache == {}
    assert ModelFactory.materialize("selected", **args) is model
    assert cache == {"selected": model, "friendly": model}
    assert ModelFactory.materialize("selected", **args) is model
    assert attempts == ["chosen", "chosen"]


def make_assembler(monkeypatch, children, *, can_access=None):
    from jiuwenswarm.agents.harness.common import session_ops_service
    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(session_ops_service, "warmup_session_context", AsyncMock())
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda _sid: {})
    monkeypatch.setattr(session_metadata, "update_session_metadata", Mock())
    owner = SimpleNamespace(
        _is_session_scoped_adapter=False,
        _session_adapter_key=lambda sid: sid or "default",
        _session_instance_config={},
        _session_instance_mode="agent",
        _session_instance_sub_mode=None,
        _session_instance_extra_create_kwargs=lambda: {},
        _new_session_scoped_adapter=Mock(side_effect=children),
        _load_skill_retrieval_session_profile=lambda _sid: None,
        _touch_session_adapter=Mock(),
        _reload_session_adapter_if_stale=AsyncMock(),
        _drain_history_preparation=AsyncMock(),
        _model_selection_can_access=can_access,
        _requested_model_name=lambda request: request.params["model_name"],
        _is_cron_execution_session=lambda _sid: False,
    )
    return SessionAssembler(owner, configuration=lambda: {}, load_binding=lambda _sid: None), owner


def make_child():
    return SimpleNamespace(
        _instance=object(),
        _permission_state=SimpleNamespace(
            permission_isolated=False,
            permission_cleanup_complete=True,
        ),
        configure_initial_execution=Mock(),
        restore_skill_retrieval_session=Mock(),
        persist_skill_retrieval_session_profile=Mock(),
        mark_session_mcp_reconcile_started=Mock(),
        create_instance=AsyncMock(),
        start_interaction=AsyncMock(),
        cleanup=AsyncMock(),
        _is_session_live=lambda _sid: False,
        _has_live_root_permission_owner=lambda _sid: False,
        _moa_enabled=False,
        _moa_fingerprint=None,
    )


@pytest.mark.asyncio
async def test_session_assembly_singleflight_publishes_one_complete_owner(monkeypatch):
    child = make_child()
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(*_args, **_kwargs):
        entered.set()
        await release.wait()

    child.create_instance = create
    assembler, owner = make_assembler(monkeypatch, [child])
    first = asyncio.create_task(assembler.ensure("same-session"))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(assembler.ensure("same-session"))
    await asyncio.sleep(0)
    assert assembler.adapters == {} and not second.done()
    release.set()
    assert await first is child and await second is child
    owner._new_session_scoped_adapter.assert_called_once_with("same-session")
    child.start_interaction.assert_awaited_once_with(session_id="same-session")
    assert assembler.adapters == {"same-session": child}


@pytest.mark.asyncio
async def test_history_preparation_keeps_construction_before_interaction(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    child = make_child()
    child._instance = None
    order = []

    async def construct(*_args, **_kwargs):
        order.append("construct")
        child._instance = object()

    async def start(*, session_id):
        assert child._instance is not None
        order.append("start")

    child.create_instance = construct
    child.start_interaction = start
    monkeypatch.setattr(session_ops_service, "prefetch_session_history", lambda *_args, **_kwargs: {})
    assembler, _owner = make_assembler(monkeypatch, [child])
    result = await asyncio.wait_for(
        assembler.ensure("history-session", history_before_request_id="first-request"), 2,
    )
    assert result is child
    assert order == ["construct", "start"]


@pytest.mark.asyncio
async def test_failed_required_assembly_cleans_child_and_allows_another_attempt(
    monkeypatch,
):
    failed, replacement = make_child(), make_child()
    failed.start_interaction.side_effect = RuntimeError("required rail not ready")
    assembler, _owner = make_assembler(monkeypatch, [failed, replacement])
    with pytest.raises(RuntimeError, match="required rail"):
        await assembler.ensure("retry-session")
    assert assembler.adapters == {}
    failed.cleanup.assert_awaited_once()
    assert not assembler.locks["retry-session"].locked()
    assert await assembler.ensure("retry-session") is replacement
    replacement.cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("self_cancel", [False, True])
async def test_failed_child_remains_owned_until_cleanup_really_completes(
    monkeypatch, self_cancel
):
    failed, replacement = make_child(), make_child()
    failed.start_interaction.side_effect = ValueError("required setup failed")
    error = (
        asyncio.CancelledError("cleanup cancelled")
        if self_cancel
        else OSError("cleanup busy")
    )
    failed.cleanup.side_effect = [error, error, None]
    assembler, owner = make_assembler(monkeypatch, [failed, replacement])
    with pytest.raises(ValueError, match="required setup"):
        await assembler.ensure("retained-owner")
    assert assembler.adapters == {}
    assert assembler.failed_cleanup == {"retained-owner": failed}
    with pytest.raises(RuntimeError, match="session_cleanup_pending"):
        await assembler.ensure("retained-owner")
    assert owner._new_session_scoped_adapter.call_count == 1
    assert await assembler.ensure("retained-owner") is replacement
    assert assembler.failed_cleanup == {}
    assert failed.cleanup.await_count == 3


@pytest.mark.asyncio
async def test_parent_shutdown_drains_retained_failed_child_and_rejects_new_assembly(
    monkeypatch,
):
    failed = make_child()
    failed.create_instance.side_effect = ValueError("create failed")
    failed.cleanup.side_effect = [OSError("resource busy"), None]
    assembler, owner = make_assembler(monkeypatch, [failed])
    with pytest.raises(ValueError, match="create failed"):
        await assembler.ensure("failed-at-shutdown")
    await assembler.cleanup_children()
    assert assembler.failed_cleanup == {} and assembler.adapters == {}
    assert failed.cleanup.await_count == 2
    with pytest.raises(RuntimeError, match="assembler is closing"):
        await assembler.ensure("new-after-close")
    assert owner._new_session_scoped_adapter.call_count == 1


@pytest.mark.asyncio
async def test_facade_retains_failed_adapter_until_a_successful_cleanup():
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    adapter = SimpleNamespace(
        cleanup=AsyncMock(side_effect=[OSError("cleanup busy"), None])
    )
    facade = object.__new__(JiuWenSwarm)
    facade._session_manager = SimpleNamespace(close_all_sessions=AsyncMock())
    facade._adapter = adapter
    with pytest.raises(OSError, match="cleanup busy"):
        await facade.cleanup()
    assert facade._adapter is adapter
    await facade.cleanup()
    assert facade._adapter is None and adapter.cleanup.await_count == 2


@pytest.mark.asyncio
async def test_sdk_stop_failure_retains_model_lease_until_stop_succeeds():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    child = object.__new__(JiuWenSwarmDeepAdapter)
    child._is_session_scoped_adapter = True
    child._parent_session_id = "stop-barrier"
    child._cleanup_evolution_background_tasks = AsyncMock()
    child.release_subagent_runtime_for_session = AsyncMock()
    child.stop_interaction = AsyncMock(side_effect=[OSError("SDK still running"), None])
    child._trusted_search_urls = SimpleNamespace(dispose=Mock())
    child._finalize_external_memory_session = AsyncMock()
    child._sync_personal_context_rail = AsyncMock()
    child._teardown_agent_owned_tools = Mock()
    child._release_sys_operations = Mock()
    child._memory_reindex_task = None
    child._mcp_prewarm_task = None
    child._close_a2x_client = AsyncMock()
    child._mcp_kernel_leases = {}
    model_lease = SimpleNamespace(release=AsyncMock())
    child._kernel_leases = {"model": model_lease}
    child._instance = None
    with pytest.raises(OSError, match="SDK still running"):
        await child.cleanup()
    assert child._kernel_leases == {"model": model_lease}
    model_lease.release.assert_not_awaited()
    await child.cleanup()
    model_lease.release.assert_awaited_once()
    assert child._kernel_leases == {}


@pytest.mark.asyncio
async def test_shutdown_joins_inflight_assembly_before_disposing_its_child(monkeypatch):
    child = make_child()
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(*_args, **_kwargs):
        entered.set()
        await release.wait()

    child.create_instance = create
    assembler, _owner = make_assembler(monkeypatch, [child])
    assembly = asyncio.create_task(assembler.ensure("inflight"))
    await asyncio.wait_for(entered.wait(), 1)
    shutdown = asyncio.create_task(assembler.cleanup_children())
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    assert await assembly is child
    await shutdown
    child.cleanup.assert_awaited_once()
    assert assembler.adapters == {} and assembler.failed_cleanup == {}


@pytest.mark.asyncio
async def test_session_assembly_passes_original_model_authorization_and_refuses_denied_selection(
    monkeypatch,
):
    child = make_child()
    request = SimpleNamespace(params={"model_name": "model:restricted"})
    checked = []

    def reject_model(*args, **kwargs):
        checked.append((args, kwargs))
        return False

    def bind(*, model_name, request, can_access):
        child.initial_selection = model_name, request, can_access

    async def create(*_args, **_kwargs):
        _model_name, original_request, can_access = child.initial_selection
        if not can_access(original_request, "restricted"):
            raise PermissionError("model access denied")

    child.configure_initial_execution = bind
    child.create_instance = create
    assembler, _owner = make_assembler(monkeypatch, [child], can_access=reject_model)
    with pytest.raises(PermissionError, match="model access denied"):
        await assembler.ensure("protected-session", execution_request=request)
    assert child.initial_selection == ("model:restricted", request, reject_model)
    assert checked == [((request, "restricted"), {})]
    assert assembler.adapters == {}
    child.start_interaction.assert_not_awaited()
    child.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_preparation_keeps_lock_until_cleanup_despite_repeated_cancel(
    monkeypatch,
):
    from jiuwenswarm.agents.harness.common import session_ops_service

    child = make_child()
    preparing, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def restore(**_kwargs):
        preparing.set()
        await asyncio.Event().wait()

    async def cleanup():
        cleaning.set()
        await release.wait()

    assembler, _owner = make_assembler(monkeypatch, [child])
    monkeypatch.setattr(session_ops_service, "warmup_session_context", restore)
    child.cleanup = cleanup
    operation = asyncio.create_task(assembler.ensure("cancelled-session"))
    await asyncio.wait_for(preparing.wait(), 1)
    assert assembler.adapters == {}, "context restoration must precede publication"
    operation.cancel("disconnected")
    await asyncio.wait_for(cleaning.wait(), 1)
    operation.cancel("shutdown")
    await asyncio.sleep(0)
    assert not operation.done() and assembler.locks["cancelled-session"].locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 1)
    assert (
        assembler.adapters == {} and not assembler.locks["cancelled-session"].locked()
    )


@pytest.mark.asyncio
async def test_model_retirement_preserves_shared_connections_until_kernel_shutdown(
    monkeypatch,
):
    from jiuwenswarm.runtime import kernel as kernel_module

    kernel = kernel_module.ResourceKernel(max_idle=0)
    monkeypatch.setattr(kernel_module, "process_kernel", lambda: kernel)
    closed = []

    class Provider:
        @classmethod
        def connection_key(cls, config):
            return config.connection

        @classmethod
        async def aclose_connections(cls, configs):
            closed.extend(config.connection for config in configs)

    def build(*_args):
        return SimpleNamespace(
            _client=Provider(), model_client_config=SimpleNamespace(connection="shared")
        )

    identity = kernel_module.KernelIdentity(
        "owner", "data", "project", "agent", "definition", "caps", "config"
    )
    first, second = ModelFactory(build), ModelFactory(build)
    first.acquire(identity, {"model_name": "one"}, {}, resource_id="one")
    second.acquire(identity, {"model_name": "two"}, {}, resource_id="two")
    kernel.retire(lambda key: key.kind == "model" and key.resource_id == "one")
    await next(iter(first.leases.values())).release()
    assert closed == [], "retiring one model cannot close another model's HTTP pool"
    await next(iter(second.leases.values())).release()
    assert closed == [], (
        "the process lifetime owner must survive ordinary idle eviction"
    )
    await kernel.close()
    assert closed == ["shared"]


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_default_prewarm_and_authorized_stable_selection_share_owned_model(
    monkeypatch, group
):
    from jiuwenswarm.common.model_catalog import ModelCatalog
    from jiuwenswarm.common.model_errors import ModelSelectionError
    from jiuwenswarm.runtime import kernel as kernel_module
    from jiuwenswarm.server.runtime import (
        model_compiler_adapter,
        model_routing_registry,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    config = {
        "models": {
            "defaults": [
                {
                    "model_id": "mdl_selected",
                    "is_default": True,
                    "model_client_config": {
                        "model_name": "chosen",
                        "client_provider": "OpenAI",
                        "api_base": "https://provider.invalid",
                        "api_key": "first-key",
                    },
                }
            ],
            "groups": [
                {
                    "model_group_id": "mgp_selected",
                    "is_default": group,
                    "routes": [{"route_id": "primary", "model_id": "mdl_selected"}],
                }
            ],
        }
    }
    catalog = ModelCatalog(config)
    real_resolver = model_routing_registry.ModelSelectionResolver
    monkeypatch.setattr(
        model_routing_registry,
        "ModelSelectionResolver",
        lambda selected_catalog=None: real_resolver(selected_catalog or catalog),
    )
    kernel = kernel_module.ResourceKernel(max_idle=0)
    monkeypatch.setattr(kernel_module, "process_kernel", lambda: kernel)
    closed, built, access = [], [], []

    class Provider:
        @classmethod
        def connection_key(cls, client):
            return client.connection

        @classmethod
        async def aclose_connections(cls, clients):
            closed.extend(client.connection for client in clients)

    def build(resolved):
        selected = resolved.routes[0].model if group else resolved
        model = SimpleNamespace(
            _client=Provider(),
            model_client_config=SimpleNamespace(connection=selected.api_key),
        )
        built.append(model)
        return model

    monkeypatch.setattr(model_compiler_adapter, "build_model_from_selection", build)
    identity = kernel_module.KernelIdentity(
        "owner", "data", "project", "agent", "def", "cap", "cfg"
    )
    warm_owner = object.__new__(JiuWenSwarmDeepAdapter)
    warm_owner._kernel_identity = identity
    warm_owner._configured_default_group_id = "mgp_selected" if group else ""
    warm_owner._configured_default_model_keys = ["chosen#0"]
    warm_owner._model_recipes = {"chosen#0": config["models"]["defaults"][0]}
    warm_owner._get_model_factory().configuration = config
    request_owner = SimpleNamespace(_kernel_identity=identity)
    request_factory = ModelFactory(lambda *_: None)
    requested = "model_group:mgp_selected" if group else "model:mdl_selected"
    warmed = warm_owner._configured_default_model()

    def allow(kind, resource_id):
        access.append((kind, resource_id))
        return True

    assert request_factory.resolve_selection(request_owner, requested, allow) is warmed
    assert access == (
        [("model_group", "mgp_selected"), ("model", "mdl_selected")]
        if group
        else [("model", "mdl_selected")]
    )
    with pytest.raises(ModelSelectionError) as denied:
        request_factory.resolve_selection(request_owner, requested, lambda *_: False)
    assert denied.value.code == "MODEL_SELECTION_FORBIDDEN"
    assert len(built) == 1, "a warm hit must not compile another provider"
    catalog.snapshot["defaults"][0]["model_client_config"]["api_key"] = "changed-key"
    replacement = request_factory.resolve_selection(request_owner, requested, allow)
    assert replacement is not warmed and len(built) == 2
    assert closed == []
    for factory in (warm_owner._get_model_factory(), request_factory):
        for lease in factory.leases.values():
            await lease.release()
    assert closed == [], "a model release cannot close another model's shared pool"
    await kernel.close()
    assert sorted(closed) == ["changed-key", "first-key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("deny", [False, True])
async def test_injected_memory_hooks_own_before_execution_and_completed_turn(
    monkeypatch, deny
):
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
    from gateway.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.runtime.agent_adapter import interface as module

    calls, writes = [], []

    class Hooks:
        async def before_memory(self, request):
            calls.append(("before", request.request_id))
            if deny:
                raise PermissionError("memory scope denied")
            return "authorized memory"

        async def after_memory(self, request, answer):
            assert any(record.get("event_type") == "chat.final" for record in writes)
            calls.append(("after", request.request_id, answer))

    class Adapter:
        async def process_message_stream_impl(self, request, inputs):
            assert inputs["memory_block"] == "authorized memory"
            calls.append(("execute", request.request_id))
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id="web",
                payload={"event_type": "chat.final", "content": "answer"},
                is_complete=False,
            )

    async def history_io(function, **record):
        function(**record)

    def forbid_registry():
        pytest.fail("execution must use the injected hook service")

    monkeypatch.setattr(ExtensionRegistry, "get_instance", forbid_registry)
    monkeypatch.setattr(
        module, "restore_chat_send_equipment_params", lambda *_args: None
    )
    monkeypatch.setattr(module, "_run_history_io", history_io)
    monkeypatch.setattr(
        module, "append_history_record", lambda **record: writes.append(record)
    )
    monkeypatch.setattr(module, "is_auto_memory_enabled", lambda *_args: False)
    swarm = object.__new__(module.JiuWenSwarm)
    swarm._sdk_name = "test"
    swarm._session_manager = SimpleNamespace(get_session_id=lambda value: value)
    swarm._ensure_adapter = lambda **_kwargs: Adapter()
    swarm._build_inputs = lambda _request: (
        {"query": "hello"},
        "cloud",
        SimpleNamespace(text="hello"),
    )
    swarm.reconcile_session_mcp = AsyncMock()
    swarm._make_retry_without_a2ui_call = lambda **_kwargs: None
    swarm.set_hook_services(Hooks())
    request = AgentRequest(
        request_id="hook-request",
        session_id="hook-session",
        channel_id="web",
        params={"mode": "agent", "query": "hello"},
        is_stream=True,
    )
    if deny:
        with pytest.raises(PermissionError, match="memory scope denied"):
            _chunks = [chunk async for chunk in swarm.process_message_stream(request)]
        assert calls == [("before", "hook-request")]
        assert [record["role"] for record in writes] == ["user"]
    else:
        chunks = [chunk async for chunk in swarm.process_message_stream(request)]
        assert chunks[-1].is_complete
        assert calls == [
            ("before", "hook-request"),
            ("execute", "hook-request"),
            ("after", "hook-request", "answer"),
        ]
