# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Single-session MoA selection, rebuild, and history restoration boundaries."""

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.session_provisioner import SessionCreateInput
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.permission_runtime_state import SessionPermissionState
from jiuwenswarm.server.runtime.session.session_info import to_session_info


GROUP = {"type": "model_group", "id": "group-a"}
MODEL = {"type": "model", "id": "model-a"}


@pytest.fixture
def router(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime.session import session_metadata

    config = {"version": 1, "experimental": {"multi_model_collaboration_enabled": True}}
    config["models"] = {
        "defaults": [{"model_id": "model-a", "is_default": True,
                      "model_client_config": {"model_name": "current"}}],
        "groups": [{"model_group_id": identifier,
                    "routes": [{"route_id": "primary", "model_id": "model-a"}]}
                   for identifier in ("group-a", "group-b")],
    }
    metadata, built = {}, []
    warmup = AsyncMock()
    monkeypatch.setattr(interface_deep, "get_config", lambda: deepcopy(config))
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda sid: deepcopy(metadata.get(sid, {})))
    def update(*, session_id, moa_enabled, **kwargs):
        assert kwargs["touch_last_message_at"] is False
        metadata.setdefault(session_id, {})["moa_enabled"] = moa_enabled
        if "model" in kwargs:
            metadata[session_id]["model"] = kwargs["model"]
    monkeypatch.setattr(session_metadata, "update_session_metadata", update)
    def select(sid, selection):
        metadata.setdefault(sid, {})["model_selection"] = deepcopy(selection)
    monkeypatch.setattr(session_metadata, "update_session_model_selection", select)
    monkeypatch.setattr("jiuwenswarm.server.runtime.moa_runtime.resolve_moa_settings", lambda value, selection, **kwargs: ({}, None, f'{value["version"]}:{selection.id}'))
    monkeypatch.setattr("jiuwenswarm.agents.harness.common.session_ops_service.warmup_session_context", warmup)
    parent = JiuWenSwarmDeepAdapter()
    parent._session_instance_mode = "agent.work.normal"
    parent._session_instance_config = {"channel_id": "web"}
    parent._load_skill_retrieval_session_profile = lambda _: None
    parent._reload_session_adapter_if_stale = AsyncMock()
    parent._is_cron_execution_session = lambda _: False
    def new_child(sid):
        child = SimpleNamespace(
            _instance=object(), _permission_state=SessionPermissionState(),
            _session_mcp_reconcile_started=True, _initial_skill_scan_mcp_names=frozenset(),
            _skill_retrieval_session_enabled=False, _moa_enabled=False, _moa_fingerprint=None,
            _enable_auto_permission=False, _restored_permission_fresh_input=lambda _: False,
            _uses_smart_permission_lifecycle=lambda _: False, _is_cron_execution=False,
            busy=False, build_overrides=None, cleanup=AsyncMock(), start_interaction=AsyncMock(),
            configure_initial_execution=Mock(), create_count=0,
            restore_skill_retrieval_session=lambda *_: None,
            persist_skill_retrieval_session_profile=lambda: None,
            mark_session_mcp_reconcile_started=lambda: None,
            _register_session_agent_task=lambda _: None,
            _requested_model_name=parent._requested_model_name,
        )
        child._is_session_live = lambda _: child.busy
        child._has_live_root_permission_owner = lambda _: False
        async def create(overrides, **kwargs):
            assert parent._session_adapter_locks[sid].locked()
            assert kwargs["mode"] == parent._session_instance_mode
            child.build_overrides = overrides
            child.create_count += 1
            child._instance_overrides = overrides
            child._moa_enabled = overrides["moa_enabled"]
            if child._moa_enabled:
                child._moa_fingerprint = f'{overrides["moa_config_snapshot"]["version"]}:{overrides["moa_model_selection"]["id"]}'
        child.create_instance = create
        built.append(child)
        return child
    parent._new_session_scoped_adapter = new_child
    return parent, config, metadata, built, warmup


@pytest.mark.asyncio
async def test_first_turn_group_precedes_build_and_restores_history(router):
    parent, config, metadata, built, warmup = router
    child = await parent._get_or_create_session_adapter("chat-a", model_selection=GROUP, history_before_request_id="current")
    assert child is built[0] and child.build_overrides["moa_enabled"] is True
    assert child.create_count == 1
    assert child.build_overrides["moa_config_snapshot"] == config
    assert metadata["chat-a"]["moa_enabled"] is True
    assert metadata["chat-a"]["model_selection"]["id"] == GROUP["id"]
    warmup.assert_awaited_once_with(deep_agent=child._instance, session_id="chat-a", history_before_request_id="current")


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_source", ["request", "metadata", "cached"])
async def test_external_harness_ignores_invalid_moa_selection(
    router: tuple,
    monkeypatch: pytest.MonkeyPatch,
    binding_source: str,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    parent, config, metadata, built, warmup = router
    session_id = "external-chat"
    binding = {
        "kind": "external_harness",
        "agent_template_name": "codex-expert",
        "provider_name": "codex",
    }
    config["experimental"]["multi_model_collaboration_enabled"] = False
    metadata[session_id] = {"model_selection": {"type": "model_group", "id": "deleted-group"}}
    monkeypatch.setattr(interface_deep, "persist_external_harness_binding", lambda *_: None)
    monkeypatch.setattr(
        interface_deep,
        "load_persisted_external_harness_binding",
        AsyncMock(side_effect=lambda _: binding if binding_source == "metadata" else None),
    )
    if binding_source == "cached":
        existing = parent._new_session_scoped_adapter(session_id)
        existing._external_harness_binding = binding
        parent._session_adapters[session_id] = existing

    child = await parent._get_or_create_session_adapter(
        session_id,
        external_harness_binding=binding if binding_source == "request" else None,
        model_selection={"type": "invalid", "id": "missing-group"},
        moa_enabled=True,
    )

    assert child is built[0]
    assert len(built) == 1
    assert child._external_harness_binding == binding
    assert child.build_overrides is None
    assert metadata[session_id]["model_selection"]["id"] == "deleted-group"
    child.start_interaction.assert_not_awaited()
    child.cleanup.assert_not_awaited()
    warmup.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_and_configuration_switch_rebuild_under_session_lock(router):
    parent, config, metadata, built, warmup = router
    first = await parent._get_or_create_session_adapter("chat-a", model_selection=GROUP)
    other = await parent._get_or_create_session_adapter("chat-b", model_selection=MODEL)
    assert await parent._get_or_create_session_adapter("chat-a") is first
    config["version"] = 2
    refreshed = await parent._get_or_create_session_adapter("chat-a")
    assert refreshed is not first and refreshed._moa_fingerprint == "2:group-a"
    first.cleanup.assert_awaited_once()
    switched = await parent._get_or_create_session_adapter("chat-a", model_selection={**GROUP, "id": "group-b"})
    assert switched is not refreshed and switched._moa_fingerprint == "2:group-b"
    normal = await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL, moa_enabled=True)
    assert normal is not switched and normal._moa_enabled is False
    assert metadata["chat-a"]["moa_enabled"] is False
    assert parent._session_adapters["chat-b"] is other
    assert len(built) == warmup.await_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,selection",
    [
        ("agent", MODEL),
        ("team", MODEL),
        ("cron", MODEL),
        ("team", {**GROUP, "route_id": "primary"}),
        ("cron", {**GROUP, "route_id": "primary"}),
    ],
)
async def test_busy_ordinary_session_accepts_its_applied_stable_model(router, kind, selection):
    parent, _, metadata, built, _ = router
    if kind == "team":
        parent._session_instance_mode = "team"
    elif kind == "cron":
        parent._is_cron_execution_session = lambda _: True
    child = await parent._get_or_create_session_adapter("chat-a", model_name="current")
    model = object()
    suffix = f":{selection['route_id']}" if selection.get("route_id") else ""
    reference = f"{selection['type']}:{selection['id']}{suffix}"
    child._stable_selection_cache = {reference: ("fingerprint", model)}
    child._last_resolved_model = model
    child.busy = True
    before = deepcopy(metadata)

    if kind == "cron" and selection["type"] == "model_group":
        with pytest.raises(ValueError, match="only support a single model"):
            await parent._get_or_create_session_adapter("chat-a", model_selection=selection)
        assert metadata == before
    else:
        assert await parent._get_or_create_session_adapter("chat-a", model_selection=selection) is child
        assert metadata["chat-a"]["model_selection"] == {**selection, "route_id": selection.get("route_id")}
    assert child._collaboration_model_selection is None
    assert len(built) == 1
    child.cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["model", "group", "route", "cached_model"])
async def test_busy_ordinary_team_accepts_a_different_model_selection(router, difference):
    parent, _, metadata, built, _ = router
    parent._session_instance_mode = "team"
    child = await parent._get_or_create_session_adapter("chat-a", model_name="current")
    model = object()
    selection = {**GROUP, "route_id": "primary"}
    child._stable_selection_cache = {"model_group:group-a:primary": ("fingerprint", model)}
    child._last_resolved_model = model
    child.busy = True
    if difference == "model":
        selection = MODEL
    elif difference == "group":
        selection = {**selection, "id": "group-b"}
    elif difference == "route":
        selection = {**selection, "route_id": "other"}
    elif difference == "cached_model":
        child._last_resolved_model = object()
    before_overrides = deepcopy(child._instance_overrides)

    assert await parent._get_or_create_session_adapter("chat-a", model_selection=selection) is child
    assert metadata["chat-a"]["model_selection"] == {**selection, "route_id": selection.get("route_id")}
    assert child._instance_overrides == before_overrides
    assert not child._moa_enabled
    assert len(built) == 1
    child.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_accepted_while_idle_survives_reload_on_busy_session(router):
    parent, _, metadata, built, _ = router
    parent._session_instance_mode = "team"
    # An attach-only subscription builds the child before any model is chosen.
    child = await parent._get_or_create_session_adapter("chat-a")
    assert child._collaboration_model_selection is None

    assert await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL) is child
    assert child._collaboration_model_selection.model_dump() == metadata["chat-a"]["model_selection"]
    # A config reload rebuilds the model and empties the stable selection cache.
    child._stable_selection_cache = {}
    child._last_resolved_model = object()
    child.busy = True

    assert await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL) is child
    next_selection = {"type": "model", "id": "model-b", "route_id": None}
    assert await parent._get_or_create_session_adapter("chat-a", model_selection=next_selection) is child
    assert metadata["chat-a"]["model_selection"] == next_selection
    assert len(built) == 1
    child.cleanup.assert_not_awaited()


@pytest.fixture
def legacy_router(router, monkeypatch):
    from agent_gateway.common.model_selection import ModelSelection
    from jiuwenswarm.common import model_catalog
    from jiuwenswarm.server.runtime.session import model_selection_store

    parent, config, metadata, _, _ = router
    config["models"]["defaults"][0]["alias"] = "current-alias"
    config["models"]["defaults"].append({
        "model_id": "model-b", "is_default": False,
        "model_client_config": {"model_name": "other"},
    })
    monkeypatch.setattr(model_catalog, "get_config", lambda: deepcopy(config))

    def stored_selection(sid):
        raw = metadata.get(sid, {}).get("model_selection")
        return ModelSelection.model_validate(raw) if raw is not None else None

    monkeypatch.setattr(model_selection_store, "get_session_model_selection", stored_selection)
    return router


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,mode,permission_owner",
    [
        (False, "agent.work.normal", False),
        (None, "agent.work.normal", False),
        (True, "agent.work.normal", False),
        (False, "agent.work.normal", True),
        (False, "agent.code.normal", False),
        (False, "team", False),
        (False, "cron", False),
    ],
)
@pytest.mark.parametrize("model_id", ["model-a", "model-b"])
async def test_busy_ordinary_request_keeps_model_selection_and_persists_it(
    legacy_router: tuple,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool | None,
    mode: str,
    permission_owner: bool,
    model_id: str,
) -> None:
    parent, config, metadata, built, _ = legacy_router
    if enabled is None:
        config.pop("experimental")
    else:
        config["experimental"]["multi_model_collaboration_enabled"] = enabled
    parent._session_instance_mode = "agent.work.normal" if mode == "cron" else mode
    parent._is_cron_execution_session = lambda _: mode == "cron"
    child = await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    child.busy = not permission_owner
    child._has_live_root_permission_owner = lambda _: permission_owner
    before_overrides = deepcopy(child._instance_overrides)
    selection = {"type": "model", "id": model_id, "route_id": None}
    request = AgentRequest(
        request_id="ordinary-switch", session_id="chat-a", channel_id="web",
        params={"model_selection": selection, "query": "Continue with the selected model"},
    )

    assert await parent._get_session_adapter_for_request(request, reserve_activity=True) is child

    assert metadata["chat-a"]["model_selection"] == selection
    assert child._instance_overrides == before_overrides
    assert not child._moa_enabled
    assert len(built) == 1
    child.cleanup.assert_not_awaited()
    follow_up = AgentRequest(request_id="follow-up", session_id="chat-a", channel_id="web", params={})
    assert parent._requested_model_name(follow_up) == f"model:{model_id}"
    resolved_model = object()
    resolver = Mock(return_value=resolved_model)
    monkeypatch.setattr(parent, "_resolve_stable_model", resolver)
    assert parent._resolve_model_for_request(follow_up) is resolved_model
    resolver.assert_called_once_with(f"model:{model_id}")


@pytest.mark.asyncio
async def test_registered_ordinary_request_can_reenter_with_a_different_model(legacy_router: tuple) -> None:
    parent, config, metadata, built, _ = legacy_router
    config["experimental"]["multi_model_collaboration_enabled"] = False
    child = await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    owner = JiuWenSwarmDeepAdapter()
    child._is_session_live = owner._is_session_live
    child._register_session_agent_task = owner._register_session_agent_task
    selection = {"type": "model", "id": "model-b", "route_id": None}
    request = AgentRequest(
        request_id="registered-switch", session_id="chat-a", channel_id="web",
        params={"model_selection": selection},
    )

    owner._register_session_agent_task("chat-a")
    try:
        assert owner._is_session_live("chat-a")
        assert await parent._get_session_adapter_for_request(request, reserve_activity=True) is child
        assert metadata["chat-a"]["model_selection"] == selection
        assert not child._moa_enabled
        assert len(built) == 1
        child.cleanup.assert_not_awaited()
    finally:
        owner._unregister_session_agent_task("chat-a")
    assert not owner._is_session_live("chat-a")


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_resume", [False, True])
async def test_busy_ordinary_applied_selection_is_saved_for_later_requests(
    legacy_router: tuple,
    approval_resume: bool,
) -> None:
    parent, _, metadata, built, _ = legacy_router
    child = await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    selection = {"type": "model", "id": "model-b", "route_id": None}
    applied_model = object()
    child._stable_selection_cache = {"model:model-b": ("fingerprint", applied_model)}
    child._last_resolved_model = applied_model
    child.busy = True
    before_overrides = deepcopy(child._instance_overrides)
    params = {"model_selection": selection}
    if approval_resume:
        params["source"] = "permission_interrupt"
        params["request_id"] = "approval"
        params["answers"] = [{"card_id": "approval", "selected_options": ["allow_once"]}]
    request = AgentRequest(
        request_id="applied-model", session_id="chat-a", channel_id="web", params=params,
    )
    assert parent._is_interrupt_resume_dispatch(request.params) is approval_resume

    assert await parent._get_session_adapter_for_request(request, reserve_activity=True) is child

    assert metadata["chat-a"]["model_selection"] == selection
    assert child._instance_overrides == before_overrides
    assert not child._moa_enabled
    assert len(built) == 1
    child.cleanup.assert_not_awaited()
    follow_up = AgentRequest(request_id="follow-up", session_id="chat-a", channel_id="web", params={})
    assert parent._requested_model_name(follow_up) == "model:model-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_group_identity", [False, True])
async def test_busy_ordinary_request_cannot_enable_moa(
    legacy_router: tuple,
    same_group_identity: bool,
) -> None:
    from agent_gateway.common.model_selection import ModelSelection

    parent, config, metadata, built, _ = legacy_router
    config["experimental"]["multi_model_collaboration_enabled"] = False
    child = await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    if same_group_identity:
        # A previously applied ordinary route can share the requested group's identity.
        child._collaboration_model_selection = ModelSelection.model_validate(GROUP)
        applied_model = object()
        child._stable_selection_cache = {"model_group:group-a": ("fingerprint", applied_model)}
        child._last_resolved_model = applied_model
    child.busy = True
    config["experimental"]["multi_model_collaboration_enabled"] = True
    before = deepcopy(metadata)
    before_overrides = deepcopy(child._instance_overrides)
    request = AgentRequest(
        request_id="enable-moa", session_id="chat-a", channel_id="web",
        params={"model_selection": GROUP},
    )

    with pytest.raises(RuntimeError, match="Model selection cannot change"):
        await parent._get_session_adapter_for_request(request, reserve_activity=True)

    assert metadata == before
    assert child._instance_overrides == before_overrides
    assert not child._moa_enabled
    assert len(built) == 1
    child.cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission_owner", [False, True])
async def test_busy_moa_request_keeps_installed_snapshot_when_globally_disabled(
    legacy_router: tuple,
    permission_owner: bool,
) -> None:
    parent, config, metadata, built, _ = legacy_router
    child = await parent._get_or_create_session_adapter("chat-a", model_selection=GROUP)
    child.busy = not permission_owner
    child._has_live_root_permission_owner = lambda _: permission_owner
    before = deepcopy(metadata)
    before_overrides = deepcopy(child._instance_overrides)
    config["version"] = 2
    config["experimental"]["multi_model_collaboration_enabled"] = False
    request = AgentRequest(
        request_id="installed-moa", session_id="chat-a", channel_id="web",
        params={"model_selection": GROUP},
    )

    assert await parent._get_session_adapter_for_request(request, reserve_activity=True) is child

    request.params["model_selection"] = MODEL
    with pytest.raises(RuntimeError, match="Model selection cannot change"):
        await parent._get_session_adapter_for_request(request, reserve_activity=True)
    request.params["source"] = "permission_interrupt"
    request.params["request_id"] = "approval"
    request.params["answers"] = [{"card_id": "approval", "selected_options": ["allow_once"]}]
    assert await parent._get_session_adapter_for_request(request, reserve_activity=True) is child
    assert metadata == before
    assert child._instance_overrides == before_overrides
    assert child._moa_enabled
    assert child._moa_fingerprint == "1:group-a"
    assert len(built) == 1
    child.cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("retain_snapshot", [False, True])
async def test_ordinary_lookup_does_not_activate_changed_moa_selection(
    legacy_router: tuple,
    retain_snapshot: bool,
) -> None:
    parent, config, metadata, built, _ = legacy_router
    config["experimental"]["multi_model_collaboration_enabled"] = False
    child = await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    child.busy = not retain_snapshot
    config["experimental"]["multi_model_collaboration_enabled"] = True
    metadata["chat-a"]["model_selection"] = deepcopy(GROUP)
    before = deepcopy(metadata)
    before_overrides = deepcopy(child._instance_overrides)

    assert await parent._get_or_create_session_adapter(
        "chat-a", retain_moa_snapshot=retain_snapshot,
    ) is child

    assert metadata == before
    assert child._instance_overrides == before_overrides
    assert not child._moa_enabled
    assert len(built) == 1
    child.cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent.work.normal", "team"])
@pytest.mark.parametrize("legacy_name", ["current", "current-alias", "current#0"])
async def test_idle_selection_accepts_same_legacy_model_while_busy(legacy_router, mode, legacy_name):
    parent, _, metadata, built, _ = legacy_router
    parent._session_instance_mode = mode
    child = await parent._get_or_create_session_adapter("chat-a")
    await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    child._stable_selection_cache = {}
    child._last_resolved_model = object()
    child.busy = True
    before = deepcopy(metadata)

    assert await parent._get_or_create_session_adapter("chat-a", model_name=legacy_name) is child
    assert await parent._get_or_create_session_adapter("chat-a") is child
    assert child._collaboration_model_selection.id == MODEL["id"]
    assert metadata == before
    assert len(built) == 1
    child.cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_name", ["current", "current-alias", "current#0"])
async def test_busy_legacy_name_resolves_catalog_when_session_choice_is_absent(legacy_router, legacy_name):
    parent, _, metadata, _, _ = legacy_router
    child = await parent._get_or_create_session_adapter("chat-a")
    await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    metadata["chat-a"]["model_selection"] = None
    child.busy = True

    assert await parent._get_or_create_session_adapter("chat-a", model_name=legacy_name) is child


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent.work.normal", "team"])
@pytest.mark.parametrize("legacy_name", ["other", "other#1", "unknown"])
async def test_busy_ordinary_legacy_model_keeps_normal_routing(legacy_router, mode, legacy_name):
    parent, _, metadata, _, _ = legacy_router
    parent._session_instance_mode = mode
    child = await parent._get_or_create_session_adapter("chat-a")
    await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    metadata["chat-a"]["model_selection"] = None
    child.busy = True

    before = deepcopy(metadata)

    assert await parent._get_or_create_session_adapter("chat-a", model_name=legacy_name) is child
    assert metadata == before
    assert not child._moa_enabled
    child.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_legacy_transport_uses_persisted_model_before_display_name(legacy_router):
    parent, _, metadata, _, _ = legacy_router
    parent._session_instance_mode = "team"
    child = await parent._get_or_create_session_adapter("chat-a")
    await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    child.busy = True

    assert await parent._get_or_create_session_adapter("chat-a", model_name="other") is child
    metadata["chat-a"]["model_selection"] = {"type": "model", "id": "model-b"}
    assert await parent._get_or_create_session_adapter("chat-a", model_name="current") is child
    request = AgentRequest(
        request_id="legacy", session_id="chat-a", channel_id="web", params={"model_name": "current"},
    )
    assert parent._requested_model_name(request) == "model:model-b"


@pytest.mark.asyncio
async def test_busy_moa_snapshot_does_not_accept_legacy_ordinary_switch(legacy_router):
    parent, *_ = legacy_router
    child = await parent._get_or_create_session_adapter("chat-a", model_selection=GROUP)
    assert child._moa_enabled
    child.busy = True

    with pytest.raises(RuntimeError, match="Model selection cannot change"):
        await parent._get_or_create_session_adapter("chat-a", model_name="current")


@pytest.mark.asyncio
async def test_busy_legacy_group_route_uses_ordinary_team_routing(legacy_router):
    parent, config, metadata, _, _ = legacy_router
    parent._session_instance_mode = "team"
    route = {**GROUP, "route_id": "primary"}
    child = await parent._get_or_create_session_adapter("chat-a")
    await parent._get_or_create_session_adapter("chat-a", model_selection=route)
    assert not child._moa_enabled
    child.busy = True

    assert await parent._get_or_create_session_adapter("chat-a", model_name="current") is child
    config["models"]["groups"][0]["routes"].append({"route_id": "other", "model_id": "model-b"})
    metadata["chat-a"]["model_selection"] = {**GROUP, "route_id": "other"}
    assert await parent._get_or_create_session_adapter("chat-a", model_name="current") is child
    request = AgentRequest(
        request_id="legacy", session_id="chat-a", channel_id="web", params={"model_name": "current"},
    )
    assert parent._requested_model_name(request) == "model_group:group-a:other"


@pytest.mark.asyncio
async def test_busy_legacy_normalization_keeps_execution_request(legacy_router):
    parent, *_ = legacy_router
    child = await parent._get_or_create_session_adapter("chat-a")
    await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    child.busy = True
    child._is_cron_execution = False
    child._restored_permission_fresh_input = lambda _: False
    resolver = Mock(wraps=child._requested_model_name)
    child._requested_model_name = resolver
    request = AgentRequest(
        request_id="legacy-followup", session_id="chat-a", channel_id="web",
        params={"model_name": "current"}, metadata={"source": "legacy-client"},
    )

    assert await parent._get_or_create_session_adapter(
        "chat-a", model_name="current", execution_request=request,
    ) is child
    resolver.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_active_invocation_and_permission_resume_keep_snapshot_when_flag_changes(router):
    parent, config, _, built, _ = router
    installed = await parent._get_or_create_session_adapter("chat-a", model_selection=GROUP)
    installed.busy = True
    config["version"] = 2
    config["experimental"]["multi_model_collaboration_enabled"] = False
    assert await parent._get_or_create_session_adapter("chat-a") is installed
    assert installed._moa_fingerprint == "1:group-a"
    with pytest.raises(RuntimeError, match="executing"):
        await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    installed.busy = False
    assert await parent._get_or_create_session_adapter("chat-a", retain_moa_snapshot=True) is installed
    with pytest.raises(ValueError, match="ordinary model"):
        await parent._get_or_create_session_adapter("chat-a")
    assert len(built) == 1
    restored = await parent._get_or_create_session_adapter("chat-a", model_selection=MODEL)
    assert restored is not installed and restored._moa_enabled is False


@pytest.mark.asyncio
async def test_restored_selection_drives_moa_independently_of_archival_bool(router):
    parent, _, metadata, _, _ = router
    metadata["restored"] = {"moa_enabled": False, "model_selection": GROUP}
    assert (await parent._get_or_create_session_adapter("restored"))._moa_enabled is True
    metadata["old-session"] = {"moa_enabled": True}
    assert (await parent._get_or_create_session_adapter("old-session", moa_enabled=True))._moa_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_deleted_group_cold_restore_falls_back_before_moa_initialization(router, enabled):
    parent, config, metadata, _, _ = router
    config["experimental"]["multi_model_collaboration_enabled"] = enabled
    metadata["restored"] = {"model_selection": {"type": "model_group", "id": "deleted"}}
    child = await parent._get_or_create_session_adapter("restored")
    assert child._moa_enabled is False
    assert child.build_overrides["moa_model_selection"]["id"] == "model-a"


@pytest.mark.asyncio
async def test_deleted_group_keeps_running_snapshot_until_idle(router):
    parent, config, _, _, _ = router
    installed = await parent._get_or_create_session_adapter("chat-a", model_selection=GROUP)
    installed.busy = True
    config["models"]["groups"] = []
    assert await parent._get_or_create_session_adapter("chat-a") is installed
    assert installed._moa_enabled is True
    installed.busy = False
    restored = await parent._get_or_create_session_adapter("chat-a")
    assert restored is not installed
    assert restored.build_overrides["moa_model_selection"]["id"] == "model-a"


@pytest.mark.asyncio
async def test_ordinary_model_never_enhances_and_bool_cannot_bypass_disabled_flag(router):
    parent, config, _, _, _ = router
    assert (await parent._get_or_create_session_adapter("ordinary", model_selection=MODEL, moa_enabled=True))._moa_enabled is False
    config["experimental"]["multi_model_collaboration_enabled"] = False
    with pytest.raises(ValueError, match="ordinary model"):
        await parent._get_or_create_session_adapter("group", model_selection=GROUP, moa_enabled=True)
    assert (await parent._get_or_create_session_adapter("old", moa_enabled=True))._moa_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["team", "cron"])
async def test_team_group_routes_normally_and_cron_rejects_group(router, kind):
    parent, config, _, _, _ = router
    config["experimental"]["multi_model_collaboration_enabled"] = False
    if kind == "team":
        parent._session_instance_mode = "team"
    else:
        parent._is_cron_execution_session = lambda _: True
    if kind == "cron":
        with pytest.raises(ValueError, match="only support a single model"):
            await parent._get_or_create_session_adapter("ordinary", model_selection=GROUP)
    else:
        assert (await parent._get_or_create_session_adapter("ordinary", model_selection=GROUP))._moa_enabled is False


@pytest.mark.asyncio
async def test_first_mcp_reconcile_forwards_group_before_instance_build(router):
    parent, _, _, built, _ = router
    await parent._get_or_create_session_adapter("chat-a", model_selection=GROUP, pending_mcp_scan_names=set())
    assert built[0].build_overrides["moa_model_selection"]["id"] == GROUP["id"]
    assert built[0]._moa_enabled is True


# A3g：gateway.embedded.runtime 副本已删除（gateway 生产走 provider 注入的
# server 实现），双侧对等校验收敛为 server 单侧。
@pytest.mark.parametrize("runtime", ["server.runtime"])
def test_metadata_updates_keep_moa_and_ordinary_selection_independent(tmp_path, monkeypatch, runtime):
    metadata = import_module(f"jiuwenswarm.{runtime}.session.session_metadata")
    # server 版已下沉 common（server 侧仅存兼容 shim，无模块属性）
    store = import_module("jiuwenswarm.common.model_selection_store")
    projection = import_module(f"jiuwenswarm.{runtime}.session.session_info").to_session_info
    from agent_gateway.common.model_selection import ModelSelection

    monkeypatch.setattr(metadata, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(store, "get_agent_sessions_dir", lambda: tmp_path)
    sid = "moa-selection-race"
    original = {"session_id": sid, "title": "before", "moa_enabled": False}
    metadata._write_metadata_sync(sid, original)
    selection = {"type": "model_group", "id": "ordinary", "route_id": "primary"}
    store.set_session_model_selection(sid, ModelSelection.model_validate(selection))
    metadata.update_session_metadata(session_id=sid, moa_enabled=True, touch_last_message_at=False, sync_write=True)
    latest = metadata._write_metadata_sync(sid, {**original, "title": "late history"})
    assert latest["moa_enabled"] is True
    assert latest["model_selection"] == selection
    assert projection(latest)["model_selection"] == selection
    projected = projection(latest)
    projected["model_selection"]["id"] = "changed copy"
    assert latest["model_selection"] == selection
    metadata.update_session_metadata(session_id=sid, moa_enabled=False, touch_last_message_at=False, sync_write=True)
    final = metadata._write_metadata_sync(sid, latest)
    assert final["moa_enabled"] is False
    assert final["model_selection"] == selection


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", 1, {}, []])
async def test_request_flag_requires_a_boolean(router, value):
    parent, *_ = router
    with pytest.raises(ValueError, match="boolean"):
        await parent._get_or_create_session_adapter("chat", moa_enabled=value)


def test_create_input_and_public_projection_preserve_false_default():
    assert SessionCreateInput(channel_id="web").moa_enabled is None
    with pytest.raises(TypeError, match="boolean"):
        SessionCreateInput(channel_id="web", moa_enabled="true")
    assert to_session_info({})["moa_enabled"] is False
    assert to_session_info({"moa_enabled": True})["moa_enabled"] is True
    assert to_session_info({"moa_enabled": "true"})["moa_enabled"] is False


@pytest.mark.asyncio
async def test_legacy_login_choice_clears_prior_group_and_stays_ordinary_on_restore(router):
    parent, config, metadata, _, _ = router
    enhanced = await parent._get_or_create_session_adapter("chat", model_selection=GROUP)
    config["experimental"]["multi_model_collaboration_enabled"] = False
    ordinary = await parent._get_or_create_session_adapter("chat", model_name="login-model", moa_enabled=True)
    assert ordinary is not enhanced and ordinary._moa_enabled is False
    assert metadata["chat"]["model_selection"] is None
    assert metadata["chat"]["model"] == "login-model"
    assert await parent._get_or_create_session_adapter("chat") is ordinary
