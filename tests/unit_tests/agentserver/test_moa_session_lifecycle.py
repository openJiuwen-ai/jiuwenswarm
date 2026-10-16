# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Single-session MoA selection, rebuild, and history restoration boundaries."""

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
            busy=False, build_overrides=None, cleanup=AsyncMock(), start_interaction=AsyncMock(),
            restore_skill_retrieval_session=lambda *_: None,
            persist_skill_retrieval_session_profile=lambda: None,
            mark_session_mcp_reconcile_started=lambda: None,
            _register_session_agent_task=lambda _: None,
        )
        child._is_session_live = lambda _: child.busy
        child._has_live_root_permission_owner = lambda _: False
        async def create(overrides, **kwargs):
            assert parent._session_adapter_locks[sid].locked()
            assert kwargs["mode"] == parent._session_instance_mode
            child.build_overrides = overrides
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
        lambda _: binding if binding_source == "metadata" else None,
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
async def test_team_and_cron_group_continue_ordinary_routing(router, kind):
    parent, config, _, _, _ = router
    config["experimental"]["multi_model_collaboration_enabled"] = False
    if kind == "team":
        parent._session_instance_mode = "team"
    else:
        parent._is_cron_execution_session = lambda _: True
    assert (await parent._get_or_create_session_adapter("ordinary", model_selection=GROUP))._moa_enabled is False


@pytest.mark.asyncio
async def test_first_mcp_reconcile_forwards_group_before_instance_build(router):
    parent, _, _, built, _ = router
    await parent._get_or_create_session_adapter("chat-a", model_selection=GROUP, pending_mcp_scan_names=set())
    assert built[0].build_overrides["moa_model_selection"]["id"] == GROUP["id"]
    assert built[0]._moa_enabled is True


@pytest.mark.parametrize("runtime", ["server.runtime", "gateway.embedded.runtime"])
def test_metadata_updates_keep_moa_and_ordinary_selection_independent(tmp_path, monkeypatch, runtime):
    metadata = import_module(f"jiuwenswarm.{runtime}.session.session_metadata")
    store = import_module(f"jiuwenswarm.{runtime}.session.model_selection_store")
    projection = import_module(f"jiuwenswarm.{runtime}.session.session_info").to_session_info
    from jiuwenswarm.common.model_selection import ModelSelection

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
